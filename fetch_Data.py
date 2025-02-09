import requests
import pymongo
from datetime import datetime
from collections import defaultdict

# ---------------------------
# MongoDB Setup
# ---------------------------
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
def fetch_recent_contests(count=100):
    """
    Fetches the most recent 'count' contests using the Codeforces contest.list API endpoint.
    """
    url = "https://codeforces.com/api/contest.list?gym=false"
    print(f"[{datetime.now()}] Fetching recent contests")
    response = requests.get(url)
    data = response.json()
    if data.get("status") != "OK":
        print(f"Error fetching contests: {data.get('comment')}")
        return []
    contests = data.get("result", [])
    # Filter out contests that are not finished
    finished_contests = [contest for contest in contests if contest["phase"] == "FINISHED"]
    # Return the most recent 'count' contests
    return finished_contests[:count]

def fetch_contest_standings(contest_id, count=100):
    """
    Fetches the standings for a specific contest using the Codeforces contest.standings API endpoint.
    """
    url = f"https://codeforces.com/api/contest.standings?contestId={contest_id}&from=1&count={count}&showUnofficial=false"
    print(f"[{datetime.now()}] Fetching standings for contest ID: {contest_id}")
    response = requests.get(url)
    data = response.json()
    if data.get("status") != "OK":
        print(f"Error fetching standings for contest {contest_id}: {data.get('comment')}")
        return []
    standings = data.get("result", {}).get("rows", [])
    return standings

# ---------------------------
# Functions to Fetch Data
# ---------------------------
def fetch_user_status(handle, from_index=1, count=1000):
    url = f"https://codeforces.com/api/user.status?handle={handle}&from={from_index}&count={count}"
    print(f"[{datetime.now()}] Fetching submissions for user: {handle}")
    response = requests.get(url)
    data = response.json()
    if data.get("status") != "OK":
        print(f"Error fetching submissions for {handle}: {data.get('comment')}")
        return None
    return data.get("result", [])

def fetch_user_rating(handle):
    url = f"https://codeforces.com/api/user.rating?handle={handle}"
    print(f"[{datetime.now()}] Fetching rating history for user: {handle}")
    response = requests.get(url)
    data = response.json()
    if data.get("status") != "OK":
        print(f"Error fetching rating for {handle}: {data.get('comment')}")
        return None
    return data.get("result", [])

def fetch_problems():
    url = "https://codeforces.com/api/problemset.problems"
    print(f"[{datetime.now()}] Fetching problem set data from Codeforces")
    response = requests.get(url)
    data = response.json()
    if data.get("status") != "OK":
        print(f"Error fetching problems: {data.get('comment')}")
        return None
    return data.get("result", {}).get("problems", [])

# ---------------------------
# Functions to Store Data in MongoDB
# ---------------------------
def store_submissions(handle, submissions):
    count = 0
    for submission in submissions:
        sub_id = submission["id"]
        submission["user_handle"] = handle
        submissions_collection.update_one(
            {"id": sub_id, "user_handle": handle},
            {"$set": submission},
            upsert=True
        )
        count += 1
    print(f"[{datetime.now()}] Stored {count} submissions for user: {handle}")

def store_ratings(handle, ratings):
    count = 0
    for rating_entry in ratings:
        contest_id = rating_entry["contestId"]
        rating_entry["user_handle"] = handle
        ratings_collection.update_one(
            {"user_handle": handle, "contestId": contest_id},
            {"$set": rating_entry},
            upsert=True
        )
        count += 1
    print(f"[{datetime.now()}] Stored {count} rating entries for user: {handle}")

def store_problems(problems):
    count = 0
    for problem in problems:
        contest_id = problem.get("contestId")
        index = problem.get("index")
        identifier = f"{contest_id}-{index}" if contest_id and index else problem.get("name")
        problem["_id"] = identifier
        problems_collection.update_one(
            {"_id": identifier},
            {"$set": problem},
            upsert=True
        )
        count += 1
    print(f"[{datetime.now()}] Stored {count} problems.")

# ---------------------------
# Main ETL Routine
# ---------------------------
def main():
    # Step 1: Fetch recent contests
    recent_contests = fetch_recent_contests(100)
    
    # Step 2: Aggregate user performance
    user_performance = defaultdict(list)
    for contest in recent_contests:
        contest_id = contest["id"]
        standings = fetch_contest_standings(contest_id, count=1000)
        for row in standings:
            handle = row["party"]["members"][0]["handle"]
            rank = row["rank"]
            user_performance[handle].append(rank)

    # Step 3: Rank users based on performance (lower average rank is better)
    ranked_users = sorted(user_performance.keys(), key=lambda user: sum(user_performance[user]) / len(user_performance[user]))[:1000]
    
    print(f"[{datetime.now()}] Top 1000 users identified.")

    # Step 4: Fetch and store data for top users
    for user in ranked_users:
        submissions = fetch_user_status(user)
        store_submissions(user, submissions)

        ratings = fetch_user_rating(user)
        store_ratings(user, ratings)

    # Step 5: Fetch and store problems (optional)
    problems = fetch_problems()
    store_problems(problems)

    print(f"[{datetime.now()}] Data pipeline completed successfully.")

if __name__ == "__main__":
    main()
