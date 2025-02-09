import requests
from requests.exceptions import RequestException, JSONDecodeError
import pymongo
from datetime import datetime
import time

# ---------------------------
# MongoDB Setup
# ---------------------------
# Connect to MongoDB (adjust connection string as needed)
mongo_client = pymongo.MongoClient("mongodb://localhost:27017/")
db = mongo_client["codeforces_db"]

# Define collections:
users_collection = db["users"]             # For user profiles (if needed)
submissions_collection = db["submissions"] # For raw submission data
ratings_collection = db["ratings"]         # For user rating history
problems_collection = db["problems"]       # For problem metadata

# ---------------------------
# Functions to Fetch Data
# ---------------------------
def make_api_request(url, max_retries=3, retry_delay=5):
    """
    Makes an API request with retry logic
    """
    for attempt in range(max_retries):
        try:
            response = requests.get(url)
            response.raise_for_status()  # Raises an HTTPError for bad responses (4xx, 5xx)
            return response.json()
        except (RequestException, JSONDecodeError) as e:
            if attempt == max_retries - 1:  # Last attempt
                print(f"Failed after {max_retries} attempts: {str(e)}")
                return None
            print(f"Attempt {attempt + 1} failed. Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)

def fetch_user_status(handle, from_index=1, count=1000):
    """
    Fetches a user's submissions using the Codeforces user.status API endpoint.
    """
    url = f"https://codeforces.com/api/user.status?handle={handle}&from={from_index}&count={count}"
    print(f"[{datetime.now()}] Fetching submissions for user: {handle}")
    data = make_api_request(url)
    if data is None or data.get("status") != "OK":
        print(f"Error fetching submissions for {handle}: {data.get('comment') if data else 'Request failed'}")
        return None
    return data.get("result", [])

def fetch_user_rating(handle):
    """
    Fetches a user's rating history using the Codeforces user.rating API endpoint.
    """
    url = f"https://codeforces.com/api/user.rating?handle={handle}"
    print(f"[{datetime.now()}] Fetching rating history for user: {handle}")
    data = make_api_request(url)
    if data is None or data.get("status") != "OK":
        print(f"Error fetching rating for {handle}: {data.get('comment') if data else 'Request failed'}")
        return None
    return data.get("result", [])

def fetch_problems():
    """
    Fetches problems using the Codeforces problemset.problems API endpoint.
    """
    url = "https://codeforces.com/api/problemset.problems"
    print(f"[{datetime.now()}] Fetching problem set data from Codeforces")
    data = make_api_request(url)
    if data is None or data.get("status") != "OK":
        print(f"Error fetching problems: {data.get('comment') if data else 'Request failed'}")
        return None
    # 'problems' key holds the list of problem objects
    return data.get("result", {}).get("problems", [])

def fetch_top_users(count=100):
    """
    Fetches top rated users using the Codeforces user.ratedList API endpoint.
    """
    url = f"https://codeforces.com/api/user.ratedList?activeOnly=true"
    print(f"[{datetime.now()}] Fetching top {count} users from Codeforces")
    data = make_api_request(url)
    if data is None or data.get("status") != "OK":
        print(f"Error fetching top users: {data.get('comment') if data else 'Request failed'}")
        return None
    # Sort by rating and take top count users
    users = sorted(data.get("result", []), key=lambda x: x.get("rating", 0), reverse=True)[:count]
    return users

# ---------------------------
# Functions to Store Data in MongoDB
# ---------------------------
def store_submissions(handle, submissions):
    """
    Stores (or updates) submission records for a given user into the submissions collection.
    Uses the submission "id" as a unique identifier.
    """
    count = 0
    for submission in submissions:
        sub_id = submission["id"]
        # Upsert the submission document based on its id.
        submissions_collection.update_one(
            {"id": sub_id},
            {"$set": submission},
            upsert=True
        )
        count += 1
    print(f"[{datetime.now()}] Stored {count} submissions for user: {handle}")

def store_ratings(handle, ratings):
    """
    Stores (or updates) rating history for a user into the ratings collection.
    Uses a composite key (user_handle and contestId) as a unique identifier.
    """
    count = 0
    for rating_entry in ratings:
        contest_id = rating_entry["contestId"]
        # Add the user handle to the record for traceability.
        rating_record = {**rating_entry, "user_handle": handle}
        ratings_collection.update_one(
            {"user_handle": handle, "contestId": contest_id},
            {"$set": rating_record},
            upsert=True
        )
        count += 1
    print(f"[{datetime.now()}] Stored {count} rating entries for user: {handle}")

def store_problems(problems):
    """
    Stores (or updates) problem metadata into the problems collection.
    Each problem is uniquely identified by a combination of contestId and index.
    """
    count = 0
    for problem in problems:
        contest_id = problem.get("contestId")
        index = problem.get("index")
        # Create a unique identifier: if contestId and index exist, use "contestId-index"
        identifier = f"{contest_id}-{index}" if contest_id and index else problem.get("name")
        # Add _id field so MongoDB can enforce uniqueness.
        problem_record = {**problem, "_id": identifier}
        problems_collection.update_one(
            {"_id": identifier},
            {"$set": problem_record},
            upsert=True
        )
        count += 1
    print(f"[{datetime.now()}] Stored {count} problems.")

# ---------------------------
# Main ETL Routine
# ---------------------------
def main():
    # Number of top users to fetch
    num_users = 100  # Adjust this number as needed
    
    # First fetch and store problems (only needs to be done once)
    # problems = fetch_problems()
    # if problems is not None:
    #     store_problems(problems)

    # Fetch top users
    top_users = fetch_top_users(num_users)
    if top_users is None:
        print("Failed to fetch top users")
        return

    # Process each user
    for user in top_users:
        handle = user["handle"]
        print(f"\nProcessing user: {handle}")
        
        # Fetch and store user submissions
        submissions = fetch_user_status(handle)
        if submissions is not None:
            store_submissions(handle, submissions)
        
        # Fetch and store user rating history
        ratings = fetch_user_rating(handle)
        if ratings is not None:
            store_ratings(handle, ratings)
            
        # Add a small delay to avoid hitting API rate limits
        time.sleep(2)

    print(f"[{datetime.now()}] Data fetch and storage complete for {num_users} users.")

if __name__ == "__main__":
    main()
