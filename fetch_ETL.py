import asyncio
import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient
import logging
from tqdm.asyncio import tqdm_asyncio
from datetime import datetime, timezone
from collections import defaultdict
from tenacity import retry, stop_after_attempt, wait_exponential, wait_random, retry_if_exception_type
from pymongo import UpdateOne, ReadPreference
from pymongo.write_concern import WriteConcern
import os
import gzip
import pickle
import concurrent.futures
from itertools import islice
import time
import contextlib
from typing import Dict, Set
import asyncio.locks
from contextlib import asynccontextmanager
from aiohttp import ClientTimeout, TCPConnector
from aiohttp.client_exceptions import ClientError, ServerTimeoutError
import backoff

# ---------------------------
# Configuration
# ---------------------------
MAX_CONCURRENT_REQUESTS = 10
REQUEST_DELAY = 0.5
TOP_USERS_LIMIT = 10000
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'

# ---------------------------
# Logging Setup
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler("etl_pipeline.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------
# MongoDB Setup
# ---------------------------
# MongoDB connection options
mongo_options = {
    'maxPoolSize': 100,
    'minPoolSize': 20,
    'maxIdleTimeMS': 60000,
    'connectTimeoutMS': 30000,
    'serverSelectionTimeoutMS': 30000,
    'waitQueueTimeoutMS': 30000,
    'retryWrites': True,
    'retryReads': True,
    'readPreference': 'primaryPreferred',  # Changed from ReadPreference.PRIMARY_PREFERRED
    'appName': 'CFHelper_ETL',
    'socketTimeoutMS': 60000,
    'heartbeatFrequencyMS': 10000,
    'maxConnecting': 50,
    # Removed loadBalanced option to avoid specifying multiple hosts with loadBalanced
    # 'loadBalanced': True
}

mongo_client = AsyncIOMotorClient(
    "mongodb+srv://20bce057:BFyS1ukytGE4Fvbp@devasy23.a8hxla5.mongodb.net/",
    **mongo_options
)

# Moved MongoHealthCheck definition before its usage
class MongoHealthCheck:
    def __init__(self, client):
        self.client = client
        self.last_check = 0
        self.check_interval = 60  # seconds

    async def check_connection(self):
        current_time = time.time()
        if current_time - self.last_check < self.check_interval:
            return True
        try:
            await self.client.admin.command('ping')
            self.last_check = current_time
            return True
        except Exception as e:
            logger.error(f"MongoDB connection check failed: {str(e)}")
            return False

    async def ensure_connected(self):
        retry_count = 0
        max_retries = 3
        base_delay = 1
        while retry_count < max_retries:
            if await self.check_connection():
                return True
            retry_count += 1
            if retry_count < max_retries:
                delay = base_delay * (2 ** (retry_count - 1))
                logger.warning(f"Retrying MongoDB connection in {delay} seconds...")
                await asyncio.sleep(delay)
        raise ConnectionError("Failed to establish MongoDB connection after multiple retries")

db = mongo_client["codeforces_optimized"]
health_check = MongoHealthCheck(mongo_client)

# Create write concern object with better durability
fast_write_concern = WriteConcern(
    w=1,
    j=False,
    wtimeout=10000
)

# Enable bulk writes for all collections with proper write concern
users_collection = db.get_collection("users", write_concern=fast_write_concern)
submissions_collection = db.get_collection("submissions", write_concern=fast_write_concern)
ratings_collection = db.get_collection("ratings", write_concern=fast_write_concern)
problems_collection = db.get_collection("problems", write_concern=fast_write_concern)

# ---------------------------
# Codeforces API Client
# ---------------------------
class RateLimitManager:
    def __init__(self, requests_per_second=5):
        self.requests_per_second = requests_per_second
        self.request_times = []
        self.lock = asyncio.Lock()
        
    async def wait_if_needed(self):
        async with self.lock:
            current_time = time.time()
            self.request_times = [t for t in self.request_times if current_time - t < 1.0]
            
            if len(self.request_times) >= self.requests_per_second:
                sleep_time = 1.0 - (current_time - self.request_times[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
            
            self.request_times.append(current_time)

class NetworkRetryManager:
    def __init__(self, max_retries=5, initial_delay=1):
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        
    @asynccontextmanager
    async def retry_context(self):
        retries = 0
        while True:
            try:
                yield
                break
            except (ClientError, ServerTimeoutError, asyncio.TimeoutError) as e:
                retries += 1
                if retries >= self.max_retries:
                    raise
                delay = self.initial_delay * (2 ** (retries - 1))
                logger.warning(f"Network error, retrying in {delay}s: {str(e)}")
                await asyncio.sleep(delay)

class CodeforcesAPI:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(10)  # Increased back to 10
        self.base_url = "https://codeforces.com/api"
        self.rate_limiter = RateLimitManager(requests_per_second=5)
        self.network_retry = NetworkRetryManager()
        self.timeout = ClientTimeout(total=30, connect=10, sock_read=10)
        
    @backoff.on_exception(
        backoff.expo,
        (ClientError, ServerTimeoutError, asyncio.TimeoutError),
        max_tries=5,
        max_time=300
    )
    async def fetch(self, session, endpoint, params=None):
        url = f"{self.base_url}/{endpoint}"
        await self.rate_limiter.wait_if_needed()
        
        async with self.semaphore, self.network_retry.retry_context():
            async with session.get(url, params=params, timeout=self.timeout) as response:
                if response.status == 429:
                    retry_after = int(response.headers.get('Retry-After', '5'))
                    logger.warning(f"Rate limit hit, waiting {retry_after}s")
                    await asyncio.sleep(retry_after)
                    raise ClientError("Rate limit exceeded")
                
                response.raise_for_status()
                data = await response.json()
                
                if data["status"] != "OK":
                    if "limit exceeded" in data.get("comment", "").lower():
                        await asyncio.sleep(5)
                        raise ClientError(data.get("comment"))
                    raise Exception(data.get("comment", "Unknown error"))
                
                return data["result"]

    async def get_recent_contests(self, session, count=200):
        contests = await self.fetch(session, "contest.list", params={"gym": "false"})
        return [c for c in contests if c["phase"] == "FINISHED"][:count]

    async def get_contest_standings(self, session, contest_id, count=10000):
        return await self.fetch(
            session, 
            "contest.standings",
            params={
                "contestId": contest_id,
                "from": 1,
                "count": count,
                "showUnofficial": "false"
            }
        )

    async def get_user_submissions(self, session, handle):
        try:
            return await self.fetch(session, "user.status", params={"handle": handle})
        except aiohttp.ClientResponseError as e:
            if e.status == 400:
                logger.error(f"Skipping invalid handle: {handle}")
                return []
            raise

    async def get_user_rating(self, session, handle):
        try:
            return await self.fetch(session, "user.rating", params={"handle": handle})
        except aiohttp.ClientResponseError as e:
            if e.status == 400:
                logger.error(f"Skipping invalid handle: {handle}")
                return []
            raise

    async def get_problems(self, session):
        return (await self.fetch(session, "problemset.problems"))["problems"]

    MAX_HANDLES_PER_REQUEST = 100  # Codeforces API limit for user.info
    
    async def get_users_info(self, session, handles):
        """Fetch user info in batches to avoid 400 errors"""
        users = []
        for i in range(0, len(handles), self.MAX_HANDLES_PER_REQUEST):
            batch = handles[i:i+self.MAX_HANDLES_PER_REQUEST]
            logger.debug(f"Fetching user batch {i//self.MAX_HANDLES_PER_REQUEST + 1}")
            try:
                result = await self.fetch(
                    session,
                    "user.info",
                    params={"handles": ";".join(batch)}
                )
                users.extend(result)
            except aiohttp.ClientResponseError as e:
                if e.status == 400:
                    logger.error(f"Invalid handle in batch: {batch}")
                else:
                    raise
            await asyncio.sleep(1)  # Add delay between batches
        return users

# ---------------------------
# Database Service
# ---------------------------
class MemoryOptimizedStorage:
    def __init__(self, base_dir="memory_optimized_data"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        
    def _get_chunk_path(self, chunk_id):
        return os.path.join(self.base_dir, f"chunk_{chunk_id}.gz")
    
    def save_chunk(self, chunk_id, data):
        path = self._get_chunk_path(chunk_id)
        with gzip.open(path, 'wb') as f:
            pickle.dump(data, f)
    
    def load_chunk(self, chunk_id):
        path = self._get_chunk_path(chunk_id)
        try:
            with gzip.open(path, 'rb') as f:
                return pickle.load(f)
        except FileNotFoundError:
            return None

    def clear_chunk(self, chunk_id):
        path = self._get_chunk_path(chunk_id)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

def chunk_data(data, size):
    iterator = iter(data)
    return iter(lambda: list(islice(iterator, size)), [])

class CursorManager:
    def __init__(self, max_cursors=100):
        self.max_cursors = max_cursors
        self.active_cursors: Set[str] = set()
        self.cursor_lock = asyncio.Lock()
        self.cursor_semaphore = asyncio.Semaphore(max_cursors)
    
    @contextlib.asynccontextmanager
    async def managed_cursor(self, cursor_id: str):
        try:
            await self.cursor_semaphore.acquire()
            async with self.cursor_lock:
                self.active_cursors.add(cursor_id)
            yield
        finally:
            async with self.cursor_lock:
                self.active_cursors.discard(cursor_id)
            self.cursor_semaphore.release()

# Create cursor manager instance
cursor_manager = CursorManager(max_cursors=100)

class DatabaseService:
    @staticmethod
    async def optimized_bulk_upsert(collection, data, key_fields, chunk_size=5000):
        if not data:
            return
            
        # Ensure MongoDB connection is healthy
        await health_check.ensure_connected()
            
        memory_storage = MemoryOptimizedStorage()
        total_chunks = (len(data) + chunk_size - 1) // chunk_size
        
        for chunk_idx, chunk in enumerate(chunk_data(data, chunk_size)):
            chunk_id = f"chunk_{chunk_idx}"
            cursor_id = f"{collection.name}_{chunk_id}"
            
            async with cursor_manager.managed_cursor(cursor_id):
                memory_storage.save_chunk(chunk_id, chunk)
                
                try:
                    await health_check.check_connection()
                    chunk_data = memory_storage.load_chunk(chunk_id)
                    operations = [
                        UpdateOne(
                            {field: item[field] for field in key_fields},
                            {"$set": item},
                            upsert=True
                        )
                        for item in chunk_data
                    ]
                    
                    await collection.bulk_write(operations, ordered=False)
                    logger.info(f"Processed chunk {chunk_idx + 1}/{total_chunks} for {collection.name}")
                    
                except Exception as e:
                    logger.error(f"Error processing chunk {chunk_idx}: {str(e)}")
                    if "not master" in str(e) or "connection" in str(e).lower():
                        await asyncio.sleep(5)
                        try:
                            await health_check.ensure_connected()
                            await collection.bulk_write(operations, ordered=False)
                        except Exception as retry_error:
                            logger.error(f"Retry failed for chunk {chunk_idx}: {str(retry_error)}")
                finally:
                    memory_storage.clear_chunk(chunk_id)

    @staticmethod
    async def store_users(users):
        for user in users:
            user["lastUpdated"] = datetime.now(timezone.utc)
        await DatabaseService.optimized_bulk_upsert(users_collection, users, ["handle"])

    @staticmethod
    async def store_submissions(submissions, batch_size=5000):
        processed_submissions = []
        
        def process_submission(submission):
            if isinstance(submission, dict):
                processed_sub = submission.copy()
                if "author" in submission and "members" in submission["author"]:
                    processed_sub["user_handle"] = submission["author"]["members"][0]["handle"]
                return processed_sub
            return None
        
        # Process submissions in parallel using ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor() as executor:
            processed_submissions = list(filter(None, executor.map(process_submission, submissions)))
        
        await DatabaseService.optimized_bulk_upsert(
            submissions_collection, 
            processed_submissions,
            ["id", "user_handle"],
            batch_size
        )

    @staticmethod
    async def store_ratings(ratings, batch_size=5000):
        await DatabaseService.optimized_bulk_upsert(
            ratings_collection,
            ratings,
            ["user_handle", "contestId"],
            batch_size
        )

    @staticmethod
    async def store_problems(problems):
        processed = []
        for p in problems:
            p["_id"] = f"{p['contestId']}-{p['index']}"
            processed.append(p)
        await DatabaseService.optimized_bulk_upsert(
            problems_collection, 
            processed, 
            ["_id"],
            batch_size=1000
        )

# ---------------------------
# ETL Pipeline
# ---------------------------
async def process_users(session, api, handles):
    logger.info(f"Fetching {len(handles)} user profiles in batches")
    users = await api.get_users_info(session, handles)
    if users:
        await DatabaseService.store_users(users)
    return users

async def process_contest_data(session, api, contest):
    try:
        logger.info(f"Processing contest {contest['id']}")
        standings = await api.get_contest_standings(session, contest["id"])
        return [row["party"]["members"][0]["handle"] for row in standings["rows"]]
    except Exception as e:
        logger.error(f"Failed to process contest {contest['id']}: {str(e)}")
        return []

async def clean_invalid_users():
    """Remove users that no longer exist from the database"""
    pipeline = [
        {"$lookup": {
            "from": "submissions",
            "localField": "handle",
            "foreignField": "user_handle",
            "as": "submissions"
        }},
        {"$match": {
            "submissions": {"$eq": []}
        }}
    ]
    
    invalid_users = await users_collection.aggregate(pipeline).to_list(None)
    if invalid_users:
        handles = [u["handle"] for u in invalid_users]
        await users_collection.delete_many({"handle": {"$in": handles}})
        logger.info(f"Cleaned up {len(handles)} invalid users")

async def verify_users_batch(session, api, users, batch_size=100):
    """Verify multiple users in batches"""
    valid_users = []
    
    for i in range(0, len(users), batch_size):
        batch = users[i:i + batch_size]
        try:
            response = await api.fetch(
                session,
                "user.info",
                params={"handles": ";".join(batch)}
            )
            if response:
                valid_users.extend([user["handle"] for user in response])
        except Exception as e:
            logger.warning(f"Error verifying batch {i//batch_size + 1}: {str(e)}")
            # Try individual users in the failed batch
            for user in batch:
                try:
                    response = await api.fetch(session, "user.info", params={"handles": user})
                    if response:
                        valid_users.append(user)
                except:
                    logger.warning(f"Skipping invalid user: {user}")
                    
    return valid_users

async def fetch_user_data_parallel(session, api, user):
    """Fetch submissions and ratings for a user in parallel"""
    submissions_task = api.get_user_submissions(session, user)
    ratings_task = api.get_user_rating(session, user)
    submissions, ratings = await asyncio.gather(submissions_task, ratings_task)
    return user, submissions or [], ratings or []

class CheckpointManager:
    def __init__(self, filename="etl_checkpoint.json"):
        self.filename = filename
        self.data = self.load()

    def load(self):
        try:
            with open(self.filename, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                "processed_users": [],
                "last_processed_index": 0,
                "total_submissions": 0,
                "total_ratings": 0
            }

    def save(self):
        with open(self.filename, 'w') as f:
            json.dump(self.data, f)

    def update_progress(self, processed_users, last_index, submissions_count, ratings_count):
        self.data["processed_users"].extend(processed_users)
        self.data["last_processed_index"] = last_index
        self.data["total_submissions"] += submissions_count
        self.data["total_ratings"] += ratings_count
        self.save()

    def get_unprocessed_users(self, all_users):
        processed = set(self.data["processed_users"])
        return [u for u in all_users if u not in processed]

class TempStorage:
    def __init__(self, base_dir="temp_data"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        
    def _get_batch_path(self, batch_id, data_type):
        return os.path.join(self.base_dir, f"{data_type}_batch_{batch_id}.json")
    
    def save_batch(self, batch_id, data, data_type):
        path = self._get_batch_path(batch_id, data_type)
        with open(path, 'w') as f:
            json.dump(data, f)
    
    def load_batch(self, batch_id, data_type):
        path = self._get_batch_path(batch_id, data_type)
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return None
    
    def clear_batch(self, batch_id, data_type):
        path = self._get_batch_path(batch_id, data_type)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

async def process_user_batch_optimized(session, api, users, checkpoint_mgr, batch_size=250):
    """Process users with parallel verification and data fetching"""
    semaphore = asyncio.Semaphore(30)
    memory_storage = MemoryOptimizedStorage()
    
    async def process_single_user(user):
        async with semaphore:
            try:
                # Verify and fetch data simultaneously
                verify_task = api.fetch(session, "user.info", params={"handles": user})
                data_task = fetch_user_data_parallel(session, api, user)
                
                results = await asyncio.gather(verify_task, data_task, return_exceptions=True)
                
                if isinstance(results[0], Exception) or isinstance(results[1], Exception):
                    logger.warning(f"Failed to process user {user}")
                    return None
                
                verify_result, (_, submissions, ratings) = results
                
                if verify_result and (submissions or ratings):
                    return user, submissions, ratings
                return None
                
            except Exception as e:
                logger.warning(f"Error processing user {user}: {str(e)}")
                return None
    
    for i in range(0, len(users), batch_size):
        batch = users[i:i + batch_size]
        batch_id = f"{i}_{int(time.time())}"
        
        try:
            # Process batch in parallel
            tasks = [process_single_user(user) for user in batch]
            results = await tqdm_asyncio.gather(*tasks, desc=f"Processing batch {i//batch_size + 1}")
            
            # Filter out None results and process valid data
            valid_results = [r for r in results if r is not None]
            
            all_submissions = []
            all_ratings = []
            processed_users = []
            
            for user_handle, submissions, ratings in valid_results:
                processed_users.append(user_handle)
                
                if submissions:
                    for sub in submissions:
                        if not "author" in sub:
                            sub["author"] = {"members": [{"handle": user_handle}]}
                    all_submissions.extend(submissions)
                
                if ratings:
                    for rating in ratings:
                        rating["user_handle"] = user_handle
                    all_ratings.extend(ratings)
            
            if all_submissions or all_ratings:
                # Use memory-optimized storage and parallel processing
                chunk_id = f"batch_{batch_id}"
                memory_storage.save_chunk(f"{chunk_id}_submissions", all_submissions)
                memory_storage.save_chunk(f"{chunk_id}_ratings", all_ratings)
                
                try:
                    # Store data in parallel
                    await asyncio.gather(
                        DatabaseService.store_submissions(all_submissions),
                        DatabaseService.store_ratings(all_ratings)
                    )
                    
                    # Update checkpoint after successful storage
                    checkpoint_mgr.update_progress(
                        processed_users,
                        i + batch_size,
                        len(all_submissions),
                        len(all_ratings)
                    )
                    
                finally:
                    # Clean up memory storage
                    memory_storage.clear_chunk(f"{chunk_id}_submissions")
                    memory_storage.clear_chunk(f"{chunk_id}_ratings")
                    
        except Exception as e:
            logger.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
            continue

async def create_indexes():
    try:
        # Submissions indexes
        await submissions_collection.create_index([("id", 1), ("user_handle", 1)], unique=True)
        await submissions_collection.create_index("user_handle")
        await submissions_collection.create_index("contestId")
        
        # Ratings indexes
        await ratings_collection.create_index([("user_handle", 1), ("contestId", 1)], unique=True)
        await ratings_collection.create_index("user_handle")
        
        # Problems indexes
        # Removed unique _id index creation since _id is always unique
        # await problems_collection.create_index("_id", unique=True)
        await problems_collection.create_index("contestId")
        
        # Users index
        await users_collection.create_index("handle", unique=True)
        
        logger.info("Database indexes created successfully")
    except Exception as e:
        logger.error(f"Error creating indexes: {str(e)}")

async def main():
    MAX_CONCURRENT_REQUESTS = 10  # Increased back to 10
    REQUEST_DELAY = 0.5  # Reduced delay
    logger.info("Starting ETL pipeline")
    start_time = datetime.now(timezone.utc)
    
    # Create indexes before starting the pipeline
    await create_indexes()
    
    api = CodeforcesAPI()
    
    # Configure client session with larger pool size
    connector = TCPConnector(
        limit=50,
        force_close=True,
        enable_cleanup_closed=True
        # Removed keepalive_timeout since it cannot be set with force_close=True
    )
    
    timeout = ClientTimeout(
        total=60,
        connect=10,
        sock_connect=10,
        sock_read=30
    )
    
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        raise_for_status=True
    ) as session:
        # Step 1: Get recent contests
        contests = await api.get_recent_contests(session)
        logger.info(f"Found {len(contests)} recent contests")

        # Step 2: Process contests and get participating users
        tasks = [process_contest_data(session, api, c) for c in contests]
        contest_results = await tqdm_asyncio.gather(*tasks, desc="Processing contests")
        
        # Step 3: Rank users by participation
        user_rankings = defaultdict(int)
        for handles in contest_results:
            for idx, handle in enumerate(handles):
                user_rankings[handle] += idx + 1
                
        top_users = sorted(user_rankings.keys(), 
                          key=lambda x: user_rankings[x])[:TOP_USERS_LIMIT]
        logger.info(f"Identified {len(top_users)} top users")

        # Skip separate verification step and use optimized processing
        checkpoint_mgr = CheckpointManager()
        unprocessed_users = checkpoint_mgr.get_unprocessed_users(top_users)
        logger.info(f"Found {len(unprocessed_users)} unprocessed users")
        
        # Use optimized batch processing
        await process_user_batch_optimized(session, api, unprocessed_users, checkpoint_mgr)
        
        # Step 7: Fetch and store problems
        problems = await api.get_problems(session)
        await DatabaseService.store_problems(problems)

        # Step 8: Clean up invalid users
        await clean_invalid_users()

    duration = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(f"ETL pipeline completed in {duration:.2f} seconds")

if __name__ == "__main__":
    import json
    import time
    asyncio.run(main())
