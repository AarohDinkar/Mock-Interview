import bcrypt
from google.cloud import firestore
from google.oauth2 import service_account

# Load Firestore credentials
SERVICE_ACCOUNT_KEY_PATH = "/Users/aaroh/Desktop/EDI6/edi6-449411-0e3991e8dce2.json"
PROJECT_ID = "edi6-449411"  # Replace with your actual Firestore project ID

# Initialize Firestore client
credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_KEY_PATH)
db = firestore.Client(project=PROJECT_ID, credentials=credentials)

def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

def add_user_data(db, username, password, title):
    """Adds user data to a 'users' collection in Firestore."""
    try:
        # Hash the password
        hashed_password = hash_password(password)
        
        # Reference to the user document
        user_ref = db.collection("users").document(username)

        # Check if the user already exists
        if user_ref.get().exists:
            print(f"User '{username}' already exists. Skipping.")
            return

        # Add user document to Firestore
        user_ref.set({
            "username": username,
            "password": hashed_password,
            "title": title,
            "created_at": firestore.SERVER_TIMESTAMP
        })
        print(f"User '{username}' added successfully.")
    
    except Exception as e:
        print(f"Error adding user: {e}")

if __name__ == "__main__":
    # Example user data
    username = "U-45"
    password = "demo45"
    title = "Aryan"
    
    add_user_data(db, username, password, title)
