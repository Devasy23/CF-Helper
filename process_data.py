import logging
import pymongo
import pandas as pd
from datetime import datetime
from rich import print
import time
import bisect
from collections import defaultdict

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Connect to your MongoDB instance
client = pymongo.MongoClient("mongodb://localhost:27017")
db = client['codeforces_optimized']  # replace with your DB name
db1 = client['codeforces_db']  # replace with your DB name
# Fetch data from MongoDB collections
submissions_raw = list(db1.submissions.find())
problems_raw    = list(db.problems.find())
ratings_raw     = list(db1.ratings.find())
users_raw       = list(db.users.find())

start_total = time.time()

# --- STEP 1: Flatten/Pre-process the Data ---
start_step1 = time.time()

# Flatten submissions so that nested problem fields are at the top level
def flatten_submission(sub):
    flat = {}
    flat['user_handle'] = sub.get('user_handle')
    flat['submission_time'] = sub.get('creationTimeSeconds')
    # extract problem info from nested dictionary
    problem = sub.get('problem', {})
    flat['problem_contestId'] = problem.get('contestId')
    flat['problem_index'] = problem.get('index')
    flat['problem_name'] = problem.get('name')
    flat['problem_rating'] = problem.get('rating')
    flat['problem_tags'] = problem.get('tags')
    flat['verdict'] = sub.get('verdict')
    return flat

# Process submissions into a DataFrame
submissions = [flatten_submission(s) for s in submissions_raw]
df_sub = pd.DataFrame(submissions)

# (Optional) You can also load problems/users/ratings into DataFrames
df_users = pd.DataFrame(users_raw)
df_ratings = pd.DataFrame(ratings_raw)
# For problems, if you need extra fields or a different join key
df_problems = pd.DataFrame(problems_raw)
print(df_sub.head())
print(f"Step 1 completed in {time.time() - start_step1:.2f} seconds")

# --- STEP 2: Feature Engineering ---
start_step2 = time.time()

# 1. **User's Rating at Submission Time**
#    For each submission, find the latest rating update (from ratings collection)
def get_user_rating_at_time(user_handle, sub_time):
    # Filter ratings for the given user that occurred before the submission time
    user_ratings = [r for r in ratings_raw 
                    if r.get('user_handle') == user_handle 
                    and r.get('ratingUpdateTimeSeconds') <= sub_time]
    if user_ratings:
        # Get the rating update with the maximum update time
        latest_update = max(user_ratings, key=lambda x: x['ratingUpdateTimeSeconds'])
        return latest_update.get('newRating')
    else:
        # Fallback: use the current rating from the users collection (if available)
        user_doc = next((u for u in users_raw if u.get('handle') == user_handle), None)
        return user_doc.get('rating') if user_doc else None

df_sub['user_rating'] = df_sub.apply(
    lambda row: get_user_rating_at_time(row['user_handle'], row['submission_time']),
    axis=1
)

print(df_sub.head())
# 2. **Rating Difference Feature**
#    Compare user's rating with the problem's rating (if available)
df_sub['rating_diff'] = df_sub.apply(
    lambda row: row['user_rating'] - row['problem_rating']
                if row['problem_rating'] is not None and row['user_rating'] is not None 
                else None,
    axis=1
)

# 3. **Label: Whether the Problem Was Solved**
#    A binary label indicating if the submission verdict is "OK" (i.e. solved)
df_sub['solved'] = df_sub['verdict'].apply(lambda v: 1 if v == "OK" else 0)

# 4. **User Overall Success Rate**
#    Compute the average success rate across all submissions for each user.
user_success = df_sub.groupby('user_handle')['solved'].mean().reset_index().rename(
    columns={'solved': 'user_success_rate'}
)
df_sub = df_sub.merge(user_success, on='user_handle', how='left')

# 5. **User Total Submission Count**
user_sub_count = df_sub.groupby('user_handle').size().reset_index(name='user_total_submissions')
df_sub = df_sub.merge(user_sub_count, on='user_handle', how='left')

# 6. **Tag-Based Success Rate**
#    For each submission, calculate an average success rate over the problem’s tags.
# This dictionary maps each user_handle to a dict that maps tag -> (times, cumulative_solved)
# Example: user_tag_data[user_handle][tag] = (sorted_times, cumulative_solved)
user_tag_data = defaultdict(lambda: defaultdict(list))

# Build the raw lists for each user and tag in one pass
for s in submissions_raw:
    user = s.get('user_handle')
    sub_time = s.get('creationTimeSeconds')
    solved = 1 if s.get('verdict') == "OK" else 0
    tags = s.get('problem', {}).get('tags', [])
    for tag in tags:
        user_tag_data[user][tag].append((sub_time, solved))

# Sort each list by time and precompute cumulative solved counts
for user, tags_dict in user_tag_data.items():
    for tag, sub_list in tags_dict.items():
        # Sort by submission time
        sub_list.sort(key=lambda x: x[0])
        times = []
        cum_solved = []
        total = 0
        solved_total = 0
        for t, solved in sub_list:
            total += 1
            solved_total += solved
            times.append(t)
            cum_solved.append(solved_total)
        user_tag_data[user][tag] = (times, cum_solved)

# --- Optimized tag_success_rate using binary search ---
def tag_success_rate_opt(user_handle, tag, current_time):
    data = user_tag_data.get(user_handle, {}).get(tag)
    if not data:
        return None
    times, cum_solved = data
    # Find the index where current_time would be inserted so that all elements before are < current_time
    idx = bisect.bisect_left(times, current_time)
    if idx == 0:
        return None  # no submissions before current_time
    solved_count = cum_solved[idx - 1]
    return solved_count / idx

# --- Optimized compute_avg_tag_success ---
def compute_avg_tag_success_opt(row):
    start_row = time.time()
    tags = row.get('problem_tags')
    if not tags:
        return None
    rates = []
    for tag in tags:
        rate = tag_success_rate_opt(row['user_handle'], tag, row['submission_time'])
        if rate is not None:
            rates.append(rate)
    result = sum(rates) / len(rates) if rates else None
    elapsed = time.time() - start_row
    if elapsed > 0.01:
        logger.info(f"compute_avg_tag_success_opt for user {row['user_handle']} took {elapsed:.4f} seconds")
    return result

# Apply the optimized function to your DataFrame
df_sub['avg_tag_success_rate'] = df_sub.apply(compute_avg_tag_success_opt, axis=1)
print(f"Step 2 completed in {time.time() - start_step2:.2f} seconds")

# --- STEP 3: Additional Features (Suggestions) ---
start_step3 = time.time()

# You can think about adding more features such as:
# - **Time Since Last Contest or Last Submission:** Compute the gap between submission times.
# - **Submission Count in a Recent Time Window:** For example, number of submissions in the last month.
# - **Problem Difficulty Offset:** For example, if the user's rating is much higher/lower than the problem rating.
# - **User Demographics:** Such as country or organization (if they might influence performance).
# Precompute sorted submission times per user
user_sub_times = defaultdict(list)
for s in submissions_raw:
    user = s.get('user_handle')
    sub_time = s.get('creationTimeSeconds')
    user_sub_times[user].append(sub_time)
for user in user_sub_times:
    user_sub_times[user].sort()

# Optimized version of time_since_last_submission using binary search
def time_since_last_submission_opt(user_handle, current_time):
    times = user_sub_times.get(user_handle)
    if not times:
        return None
    idx = bisect.bisect_left(times, current_time)
    if idx == 0:
        return None  # no submission before current_time
    last_time = times[idx - 1]
    return current_time - last_time

df_sub['time_since_last_sub'] = df_sub.apply(
    lambda row: time_since_last_submission_opt(row['user_handle'], row['submission_time']),
    axis=1
)

# New Feature: Recent Submission Count (e.g., submissions in the last 30 days)
def recent_submission_count_opt(user_handle, current_time, window=30*24*3600):
    times = user_sub_times.get(user_handle)
    if not times:
        return 0
    lower_bound = current_time - window
    left_idx = bisect.bisect_left(times, lower_bound)
    right_idx = bisect.bisect_left(times, current_time)
    return right_idx - left_idx

df_sub['recent_sub_count'] = df_sub.apply(
    lambda row: recent_submission_count_opt(row['user_handle'], row['submission_time']),
    axis=1
)

# New Feature: Absolute Problem Difficulty Offset
df_sub['abs_difficulty_offset'] = df_sub['rating_diff'].apply(lambda rd: abs(rd) if rd is not None else None)

# New Feature: User Demographics (e.g., country)
# Extract relevant demographics from df_users; assuming column 'country' exists and user's handle is in 'handle'
df_users_small = df_users[['handle', 'country']]  # adjust if more demographics are available
df_sub = df_sub.merge(df_users_small, left_on='user_handle', right_on='handle', how='left')
df_sub = df_sub.rename(columns={'country': 'user_country'}).drop(columns=['handle'])

print(f"Step 3 completed in {time.time() - start_step3:.2f} seconds")

# --- STEP 4: Prepare Final CSV for Model Training ---
start_step4 = time.time()

# Select only the features you want to include in your model.
# Adjust the list of columns as needed.
feature_columns = [
    'user_handle',
    'submission_time',
    'problem_contestId',
    'problem_index',
    'problem_rating',
    'rating_diff',
    'user_rating',
    'user_success_rate',
    'user_total_submissions',
    'avg_tag_success_rate',
    'time_since_last_sub',
    'recent_sub_count',
    'abs_difficulty_offset',
    # 'user_country',
    'solved'  # This is the label.
]

df_features = df_sub[feature_columns]

# Save the DataFrame to a CSV file.
df_features.to_csv("codeforces_feature_dataset_v1.1.csv", index=False)

print("CSV file with engineered features saved as codeforces_feature_dataset.csv")
print(f"Step 4 completed in {time.time() - start_step4:.2f} seconds")
print(f"Total processing time: {time.time() - start_total:.2f} seconds")
