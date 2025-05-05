# db.py
import os
from pymongo import MongoClient, ASCENDING
from dotenv import load_dotenv

load_dotenv()  # loads BOT_TOKEN, MONGO_URI

client = MongoClient(os.getenv("MONGO_URI"))
db = client.get_database("city_picker")

# Ensure indexes
db.criteria_sessions.create_index([("chat_id", ASCENDING), ("status", ASCENDING)])
db.tinder_sessions.create_index([("session_id", ASCENDING)])
db.cities.create_index([("city", ASCENDING)], unique=True)
