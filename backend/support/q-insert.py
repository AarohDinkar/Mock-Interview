### Used to insert data in firestore

import json
import os
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Firestore
cred_path = os.getenv("FIRESTORE_CREDS_PATH")
if not firebase_admin._apps:
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# Sample JSON data (replace this with your actual JSON data)
json_data = {
  "difficulty_levels": {
    "Easy": [
      {
        "id": "1",
        "question": "Given an array of integers nums and an integer target, return indices of the two numbers such that they add up to target.",
        "constraints": [
          "Each input would have exactly one solution.",
          "You may not use the same element twice."
        ],
        "examples": [
          {
            "input": "nums = [2, 7, 11, 15], target = 9",
            "output": "[0, 1]"
          },
          {
            "input": "nums = [3, 2, 4], target = 6",
            "output": "[1, 2]"
          }
        ],
        "tags": ["Array", "Hash Table"],
        "difficulty": "Easy",
      }
    ]
  }
}

def insert_data_to_firestore(data):
    try:
        difficulty_levels = data["difficulty_levels"]
        
        # Loop through each difficulty level
        for difficulty, questions in difficulty_levels.items():
            # Loop through questions in each difficulty level
            for question in questions:
                # Ensure consistent structure
                question_data = {
                    "id": question["id"],
                    "question": question["question"],
                    "constraints": question["constraints"],
                    "examples": question["examples"],
                    "tags": question["tags"],
                    "difficulty": difficulty
                }
                
                # Insert into Coding_Qs collection
                question_ref = db.collection("Coding_Qs").document()
                question_ref.set(question_data)
                print(f"Successfully inserted {difficulty} question: {question['id']}")
    except Exception as e:
        print(f"Error inserting data: {str(e)}")

# Verify Firebase connection
if db:
    print("Firebase connection established")
    # Call the function with the JSON data
    insert_data_to_firestore(json_data)
else:
    print("Firebase connection failed")