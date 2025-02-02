from flask import Flask, render_template, request, redirect, session, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
import bcrypt
from google.cloud import texttospeech_v1 as texttospeech
import os
from dotenv import load_dotenv
import anthropic
import random
import time
import logging
import subprocess
import tempfile
import json

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'default_secret_key')  # Replace with a secure key in production

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set the logging level to INFO
    format='%(asctime)s - %(levelname)s - %(message)s',  # Define the log message format
    handlers=[
        logging.FileHandler("app.log"),  # Log messages will be written to 'app.log'
        logging.StreamHandler()  # Additionally, log messages will be output to the console
    ]
)

logger = logging.getLogger(__name__)  # Create a logger instance for this module

# Load credentials from environment variables
firestore_creds_path = os.getenv('FIRESTORE_CREDS_PATH')
tts_creds_path = os.getenv('TTS_CREDS_PATH')
claude_api_key = os.getenv('CLAUDE_API_KEY')

# Initialize Firebase Admin SDK
if firestore_creds_path:
    cred = credentials.Certificate(firestore_creds_path)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Initialized Firebase Admin SDK.")
else:
    logger.error("FIRESTORE_CREDS_PATH not set in .env")
    raise Exception("FIRESTORE_CREDS_PATH not set in .env")

# Initialize Text-to-Speech Client
if tts_creds_path:
    tts_client = texttospeech.TextToSpeechClient.from_service_account_file(tts_creds_path)
    logger.info("Initialized Text-to-Speech client.")
else:
    logger.error("TTS_CREDS_PATH not set in .env")
    raise Exception("TTS_CREDS_PATH not set in .env")

# Initialize Anthropic Client for Claude API
if claude_api_key:
    anthropic_client = anthropic.Client(api_key=claude_api_key)
    logger.info("Initialized Anthropic Client for Claude API.")
else:
    logger.error("CLAUDE_API_KEY not set in .env")
    raise Exception("CLAUDE_API_KEY not set in .env")


@app.route('/')
def index():
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def login():
    username = request.form['username']
    password = request.form['password']

    try:
        # Fetch the user document from Firestore
        user_ref = db.collection('users').document(username)  # Replace 'users' with your Firestore collection name
        user_doc = user_ref.get()

        if user_doc.exists:
            user_data = user_doc.to_dict()
            stored_hashed_password = user_data.get('password')

            # Check password via bcrypt
            if stored_hashed_password and bcrypt.checkpw(password.encode('utf-8'), stored_hashed_password.encode('utf-8')):
                session['username'] = username
                session['title'] = user_data.get('title', 'Candidate')  # Store title in session, default 'Candidate'
                logger.info(f"User '{username}' logged in successfully.")
                return redirect('/info')
            else:
                logger.warning(f"Invalid credentials for user '{username}'.")
                return render_template('login.html', error='Invalid credentials')
        else:
            logger.warning(f"User '{username}' not found.")
            return render_template('login.html', error='User not found')

    except Exception as e:
        logger.error(f"An error occurred during login: {e}")
        return render_template('login.html', error='Login failed. Please try again.')


@app.route('/info')
def info():
    if 'username' not in session:
        return redirect('/login')
    # Pass the user's 'title' to the template if you like
    user_title = session.get('title', 'Candidate')
    return render_template('info.html', title=user_title)


######################################################################
# STAGE 1
######################################################################
@app.route('/start_stage1', methods=['POST'])
def start_stage1():
    if 'username' in session:
        num_questions = request.form['num_questions']
        try:
            session['num_questions'] = int(num_questions)  # Store in session
            # Initialize Stage 1 variables
            session['questions_answered'] = 0
            session['current_difficulty_range'] = None  # To track difficulty for next question
            logger.info(f"User '{session['username']}' started Stage 1 with {num_questions} questions.")
            return redirect('/stage1')
        except ValueError:
            logger.error(f"Invalid number of questions: {num_questions}")
            return redirect('/info')  # Could redirect with an error message
    return redirect('/')


@app.route('/stage1')
def stage1():
    if 'username' not in session:
        return redirect('/')
    return render_template('stage1.html')


@app.route('/get_stage1_info')
def get_stage1_info():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    return jsonify({
        "num_questions": session.get('num_questions', 1),
        "questions_answered": session.get('questions_answered', 0)
    })


@app.route('/get_next_question')
def get_next_question():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    questions_answered = session.get('questions_answered', 0)
    total_questions = session.get('num_questions', 1)
    current_difficulty_range = session.get('current_difficulty_range')

    # Check if we already completed the stage
    if questions_answered >= total_questions:
        logger.info(f"User '{session['username']}' has completed Stage 1.")
        return jsonify({
            "completed": True,
            "completedMessage": "You have completed Stage 1! Congratulations!"
        })

    # Fetch questions from Firestore
    qs_collection = db.collection('Qs')

    if current_difficulty_range:
        # Attempt to fetch questions within the difficulty range
        qs_query = qs_collection \
            .where('difficultyRange.lowerLimit', '<=', current_difficulty_range.get('rating', 0)) \
            .where('difficultyRange.upperLimit', '>=', current_difficulty_range.get('rating', 10))
    else:
        # First question: pick randomly
        qs_query = qs_collection

    qs_docs = qs_query.get()
    if not qs_docs:
        # No questions found, fallback to all docs
        qs_docs = qs_collection.get()

    if not qs_docs:
        logger.info(f"No questions available for user '{session['username']}'.")
        return jsonify({
            "completed": True,
            "completedMessage": "No more questions available."
        })

    chosen_doc = random.choice(qs_docs).to_dict()

    # Store the chosen question in session to compare with user’s answer later
    session['current_question'] = chosen_doc.get('question')
    session['current_answer'] = chosen_doc.get('answer')
    session['current_difficulty_range'] = chosen_doc.get('difficultyRange', {'lowerLimit':0.0, 'upperLimit':2.5})

    logger.info(f"Picked question for user '{session['username']}': '{session['current_question']}'")

    return jsonify({
        "completed": False,
        "question": session['current_question']
    })


@app.route('/submit_answer_stage1', methods=['POST'])
def submit_answer_stage1():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    data = request.get_json()
    user_answer = data.get('userAnswer', "")
    question = session.get('current_question', "")
    correct_answer = session.get('current_answer', "")
    questions_answered = session.get('questions_answered', 0)
    total_questions = session.get('num_questions', 1)

    # Claude system prompt
    system_prompt = """You are a strict evaluator. You receive a question and a reference correct answer.
Then you receive a user's answer. You must rate the user's answer strictly on a scale of 0 to 10
(where 0 is totally incorrect and 10 is perfectly correct).
Return ONLY the numeric rating, like "7.5" or "3.6", in plain text without extra explanation.
"""

    user_content = f"""Question: {question}
Reference Answer: {correct_answer}
User Answer: {user_answer}
Please provide numeric rating from 0 to 10.
"""

    # === Using the anthropic_client.messages.create() method per your snippet ===
    try:
        message = anthropic_client.messages.create(
            model="claude-3-sonnet-20240229",
            max_tokens=10,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_content}
            ]
        )
        rating_str = message.content[0].text.strip()
        rating = float(rating_str)

        # Log the rating and the question answered
        logger.info(f"User '{session['username']}' answered question: '{question}' with rating: {rating}")
    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        return jsonify({"error": "Failed to process answer. Please try again."}), 500
    # === End of snippet ===

    # Save user’s Q&A data + rating in Firestore
    username = session['username']
    user_doc_ref = db.collection('users').document(username).collection('stage1_answers')
    user_doc_ref.add({
        "timestamp": time.time(),
        "question": question,
        "ref_answer": correct_answer,
        "user_answer": user_answer,
        "rating": rating
    })

    questions_answered += 1
    session['questions_answered'] = questions_answered

    if questions_answered >= total_questions:
        logger.info(f"User '{username}' has completed Stage 1.")
        return jsonify({
            "completed": True,
            "completedMessage": "You have completed Stage 1! Congratulations!"
        })
    else:
        qs_collection = db.collection('Qs')
        try:
            qs_docs = list(qs_collection.stream())
            # Filter by rating
            matching_qs = [
                doc for doc in qs_docs
                if doc.to_dict().get('difficultyRange', {}).get('lowerLimit', 0) <= rating
                and doc.to_dict().get('difficultyRange', {}).get('upperLimit', 10) >= rating
            ]

            if matching_qs:
                next_doc = random.choice(matching_qs).to_dict()
                next_question = next_doc['question']
                next_answer = next_doc['answer']
                session['current_question'] = next_question
                session['current_answer'] = next_answer
                session['current_difficulty_range'] = next_doc.get('difficultyRange', {'lowerLimit':0.0, 'upperLimit':2.5})

                logger.info(f"Next question for user '{username}': '{next_question}'")
                return jsonify({
                    "completed": False,
                    "nextQuestion": next_question
                })
            else:
                if qs_docs:
                    chosen_doc = random.choice(qs_docs).to_dict()
                    next_question = chosen_doc['question']
                    next_answer = chosen_doc['answer']
                    session['current_question'] = next_question
                    session['current_answer'] = next_answer
                    session['current_difficulty_range'] = chosen_doc.get('difficultyRange', {'lowerLimit':0.0, 'upperLimit':2.5})

                    logger.info(f"Fallback question for user '{username}': '{next_question}'")
                    return jsonify({
                        "completed": False,
                        "nextQuestion": next_question
                    })
                else:
                    logger.info(f"No more questions available for user '{username}'.")
                    return jsonify({
                        "completed": True,
                        "completedMessage": "No more questions available."
                    })
        except Exception as e:
            logger.error(f"Error querying questions: {e}")
            return jsonify({"error": "Failed to fetch questions. Please try again."}), 500


@app.route('/next_stage')
def next_stage():
    # Transition from Stage 1 -> Stage 2
    if 'username' in session:
        logger.info(f"User '{session['username']}' proceeded to Stage 2.")
        return redirect('/stage2')
    else:
        logger.warning("A user attempted to proceed to Stage 2 without logging in.")
        return redirect('/')


######################################################################
# STAGE 2
######################################################################
@app.route('/stage2')
def stage2():
    # Render Stage 2: Coding Interview
    if 'username' not in session:
        return redirect('/')
    return render_template('stage2.html')


@app.route('/get_coding_question', methods=['GET'])
def get_coding_question():
    try:
        # Change collection name to match insertion script
        qs_docs = db.collection('Coding_Qs').stream()
        
        # Convert stream to list and check if empty
        questions = list(qs_docs)
        if not questions:
            logger.error("No questions found in database")
            return jsonify({"error": "No questions available"}), 404
            
        # Randomly select a question
        chosen_doc = random.choice(questions).to_dict()
        session['current_coding_question'] = chosen_doc
        
        logger.info(f"Picked coding question for user '{session['username']}': '{chosen_doc['question']}'")
        return jsonify({"question": chosen_doc})

    except Exception as e:
        logger.error(f"Error fetching coding questions: {e}")
        return jsonify({"error": "Failed to fetch coding questions."}), 500


@app.route('/run_code', methods=['POST'])
def run_code():
    # Execute the submitted code (Python or Java)
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    data = request.get_json()
    language = data.get('language')
    code = data.get('code')

    if not language or not code:
        logger.warning(f"User '{session['username']}' submitted incomplete code execution request.")
        return jsonify({"error": "Language and code are required."}), 400

    if language not in ['Python', 'Java']:
        logger.warning(f"User '{session['username']}' attempted to execute unsupported language: '{language}'.")
        return jsonify({"error": "Unsupported language. Please select Python or Java."}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdirname:
            if language == 'Python':
                file_path = os.path.join(tmpdirname, 'script.py')
                with open(file_path, 'w') as f:
                    f.write(code)
                process = subprocess.run(['python3', file_path], capture_output=True, text=True, timeout=10)
            elif language == 'Java':
                file_path = os.path.join(tmpdirname, 'Main.java')
                with open(file_path, 'w') as f:
                    f.write(code)
                compile_process = subprocess.run(['javac', file_path], capture_output=True, text=True, timeout=10)
                if compile_process.returncode != 0:
                    return jsonify({"error": f"Compilation Error:\n{compile_process.stderr}"}), 400
                class_path = tmpdirname
                process = subprocess.run(['java', '-cp', class_path, 'Main'], capture_output=True, text=True, timeout=10)

        if process.returncode != 0:
            logger.info(f"User '{session['username']}' encountered an error during code execution.")
            return jsonify({"error": f"Runtime Error:\n{process.stderr}"}), 400

        output = process.stdout.strip()
        logger.info(f"User '{session['username']}' executed code successfully.")
        return jsonify({"output": output})

    except subprocess.TimeoutExpired:
        logger.error(f"User '{session['username']}' code execution timed out.")
        return jsonify({"error": "Code execution timed out."}), 408
    except Exception as e:
        logger.error(f"Error during code execution for user '{session['username']}': {e}")
        return jsonify({"error": "An error occurred during code execution."}), 500


@app.route('/start_stage2', methods=['POST'])
def start_stage2():
    # Initialize Stage 2 variables if needed
    if 'username' in session:
        logger.info(f"User '{session['username']}' started Stage 2: Coding Interview.")
        return redirect('/stage2')
    return redirect('/')


# -------------- NEW: Finalize Code (Generate Stage 3 Qs) --------------
@app.route('/finalize_code', methods=['POST'])
def finalize_code():
    """
    1. Store user's final code in Firestore.
    2. Call Claude to generate 3 advanced questions about the code.
    3. Store these 3 questions in Firestore for Stage 3.
    4. Return success or failure.
    """
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    data = request.get_json()
    language = data.get('language', 'Python')
    code = data.get('code', '')

    if not code.strip():
        return jsonify({"error": "No code provided."}), 400

    username = session['username']

    # Step 1: Store code in Firestore under "coding_round_questions_code"
    try:
        code_doc_ref = db.collection('users').document(username).collection('coding_round_questions_code')
        code_doc_ref.add({
            "timestamp": time.time(),
            "language": language,
            "final_code": code
        })
        logger.info(f"Final code stored for user '{username}'.")
    except Exception as e:
        logger.error(f"Error storing final code: {e}")
        return jsonify({"error": "Failed to store code in Firestore."}), 500

    # Step 2: Call Claude to generate 3 questions about the code
    system_prompt = """You are an interviewer. The user has written code. Generate exactly 3 advanced conceptual questions about that code, 
focusing on edge cases, time complexity, or design. 
Return them in JSON array format, e.g.: ["Q1...","Q2...","Q3..."] 
No extra text, just JSON.
"""

    user_content = f"User's code:\n{code}"

    try:
        # Using the same snippet (messages.create)
        message = anthropic_client.messages.create(
            model="claude-3-sonnet-20240229",
            max_tokens=200,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_content}
            ]
        )
        # Attempt to parse the returned content as JSON
        # We expect something like: ["Q1...","Q2...","Q3..."]
        claude_raw = message.content[0].text.strip()
        claude_questions = json.loads(claude_raw)  # parse JSON
        if not isinstance(claude_questions, list):
            raise ValueError("Claude did not return a JSON list.")
        if len(claude_questions) != 3:
            raise ValueError("Claude did not return exactly 3 questions.")
        logger.info(f"Claude returned 3 questions for user '{username}'.")
    except Exception as e:
        logger.error(f"Error generating Stage 3 questions with Claude: {e}")
        return jsonify({"error": "Failed to generate Stage 3 questions."}), 500

    # Step 3: Store these 3 questions in Firestore (e.g. collection: 'stage3_questions')
    try:
        # Overwrite or create a new doc specifically for stage3
        stage3_ref = db.collection('users').document(username).collection('stage3_questions')
        # Let's store each question in a doc or store them together
        # We'll store them in a single doc for convenience
        stage3_ref.document("generated_questions").set({
            "questions": claude_questions,
            "timestamp": time.time()
        })
        logger.info(f"Stored stage3 questions for user '{username}'.")
    except Exception as e:
        logger.error(f"Error storing stage3 questions in Firestore: {e}")
        return jsonify({"error": "Failed to store stage3 questions."}), 500

    # Everything is good
    return jsonify({"success": True})


# -------------- Move to Stage 3 --------------
@app.route('/next_stage3')
def next_stage3():
    # Transition from Stage 2 -> Stage 3
    if 'username' in session:
        logger.info(f"User '{session['username']}' proceeded to Stage 3.")
        return redirect('/stage3')
    else:
        logger.warning("A user attempted to proceed to Stage 3 without logging in.")
        return redirect('/')


######################################################################
# STAGE 3
######################################################################
@app.route('/stage3')
def stage3():
    # Render Stage 3: Verbal Q&A about the final code
    if 'username' not in session:
        return redirect('/')
    return render_template('stage3.html')


@app.route('/get_stage3_info', methods=['GET'])
def get_stage3_info():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    username = session['username']
    # We stored stage3 questions in the doc "generated_questions" in user's "stage3_questions"
    stage3_ref = db.collection('users').document(username).collection('stage3_questions').document('generated_questions').get()
    if not stage3_ref.exists:
        return jsonify({"error": "No Stage 3 questions found"}), 404

    questions_data = stage3_ref.to_dict()
    questions = questions_data.get("questions", [])
    # Return how many questions, and how many answered so far (track in session, or we can store in user doc)
    # We'll track in session for simplicity
    if "stage3_total_qs" not in session:
        session["stage3_total_qs"] = len(questions)
    if "stage3_answered" not in session:
        session["stage3_answered"] = 0

    return jsonify({
        "total_questions": session["stage3_total_qs"],
        "answered": session["stage3_answered"]
    })


@app.route('/get_next_question_stage3')
def get_next_question_stage3():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    username = session['username']
    answered = session.get("stage3_answered", 0)
    total = session.get("stage3_total_qs", 0)

    if answered >= total:
        return jsonify({
            "completed": True,
            "completedMessage": "You have completed Stage 3! Congratulations!"
        })

    # Retrieve the stored questions
    stage3_ref = db.collection('users').document(username).collection('stage3_questions').document('generated_questions').get()
    if not stage3_ref.exists:
        return jsonify({"error": "No Stage 3 questions found"}), 404

    questions_data = stage3_ref.to_dict()
    questions = questions_data.get("questions", [])

    if answered < len(questions):
        current_question = questions[answered]
        session["current_stage3_question"] = current_question
        return jsonify({
            "completed": False,
            "question": current_question
        })
    else:
        return jsonify({
            "completed": True,
            "completedMessage": "No more questions available."
        })


@app.route('/submit_answer_stage3', methods=['POST'])
def submit_answer_stage3():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    data = request.get_json()
    user_answer = data.get('userAnswer', "")
    question = session.get('current_stage3_question', "")
    answered = session.get("stage3_answered", 0)
    total = session.get("stage3_total_qs", 0)

    if not question:
        return jsonify({"error": "No question in session."}), 400

    # For rating:
    system_prompt = """You are a strict evaluator. You receive a question and expect an advanced conceptual answer.
Then you receive a user's answer. You must rate the user's answer strictly on a scale of 0 to 10.
Return ONLY the numeric rating, like "7.5" or "3.6".
"""

    user_content = f"""Question: {question}
User Answer: {user_answer}
Please provide numeric rating from 0 to 10.
"""

    # Call Claude
    try:
        message = anthropic_client.messages.create(
            model="claude-3-sonnet-20240229",
            max_tokens=10,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_content}
            ]
        )
        rating_str = message.content[0].text.strip()
        rating = float(rating_str)
        logger.info(f"Stage3 rating for user '{session['username']}': {rating}")
    except Exception as e:
        logger.error(f"Error calling Claude API for stage3: {e}")
        return jsonify({"error": "Failed to process answer for Stage 3."}), 500

    # Store answer in Firestore
    username = session['username']
    try:
        db.collection('users').document(username).collection('stage3_answers').add({
            "timestamp": time.time(),
            "question": question,
            "user_answer": user_answer,
            "rating": rating
        })
    except Exception as e:
        logger.error(f"Error storing Stage 3 answer: {e}")

    answered += 1
    session["stage3_answered"] = answered

    if answered >= total:
        return jsonify({
            "completed": True,
            "completedMessage": "You have completed Stage 3! Congratulations!"
        })
    else:
        # Return next question
        return jsonify({"completed": False})


######################################################################
# TEXT-TO-SPEECH
######################################################################
@app.route('/synthesize_text', methods=['POST'])
def synthesize_text():
    data = request.get_json()
    text = data.get('text', "")
    language = data.get('language', 'en-US')

    if not text:
        logger.warning("No text provided for TTS synthesis.")
        return jsonify({"error": "No text provided."}), 400

    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code=language,
        name="en-US-Neural2-C"
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )

    try:
        response = tts_client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )
        logger.info(f"Synthesized text for TTS: '{text}'")
        return response.audio_content
    except Exception as e:
        logger.error(f"Error during TTS synthesis: {e}")
        return jsonify({"error": "Failed to synthesize text."}), 500


if __name__ == '__main__':
    app.run(debug=True)
