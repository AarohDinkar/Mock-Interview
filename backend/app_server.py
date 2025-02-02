from flask import Flask, render_template, request, redirect, session, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
import bcrypt
from google.cloud import texttospeech_v1 as texttospeech
import os
import anthropic
import random
import time
import logging
import subprocess
import tempfile
import json

# Import Secret Manager
from google.cloud import secretmanager as sm

app = Flask(__name__)

# Set a session secret key. Optionally store this in Secret Manager as well.
app.secret_key = 'super-secret-key-change-me-in-production'

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # or logging.DEBUG if you want more details
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# LOAD SECRETS FROM GOOGLE SECRET MANAGER
# ---------------------------------------------------------------------
try:
    project_id = os.getenv("GCP_PROJECT")  # Provided automatically on App Engine
    if not project_id:
        logger.warning("GCP_PROJECT environment variable not set. Using fallback project ID.")
        project_id = "my-fallback-project-id"  # For local dev if needed
    
    secret_client = sm.SecretManagerServiceClient()

    def get_secret(secret_id, version_id="latest"):
        """
        Retrieve the secret payload (string) from Secret Manager.
        """
        name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
        response = secret_client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    
    # Fetch secrets
    claude_api_key = get_secret("CLAUDE_API_KEY")
    firestore_creds_json = get_secret("FIRESTORE_CREDS_JSON")
    tts_creds_json = get_secret("TTS_CREDS_JSON")

    # Initialize Firebase Admin SDK (Firestore)
    firestore_creds_dict = json.loads(firestore_creds_json)
    cred = credentials.Certificate(firestore_creds_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Initialized Firebase Admin SDK with secret manager credentials.")

    # Initialize Text-to-Speech Client
    tts_creds_dict = json.loads(tts_creds_json)
    tts_client = texttospeech.TextToSpeechClient.from_service_account_info(tts_creds_dict)
    logger.info("Initialized Text-to-Speech client from secret manager credentials.")

    # Initialize Anthropic Client for Claude
    anthropic_client = anthropic.Client(api_key=claude_api_key)
    logger.info("Initialized Anthropic Client for Claude API from secret manager.")

except Exception as e:
    logger.error(f"Failed to load secrets from Secret Manager or initialize services: {e}")
    raise

# ---------------------------------------------------------------------
# ROUTES AND BUSINESS LOGIC
# ---------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def login():
    username = request.form['username']
    password = request.form['password']

    try:
        # Fetch the user document from Firestore
        user_ref = db.collection('users').document(username)
        user_doc = user_ref.get()

        if user_doc.exists:
            user_data = user_doc.to_dict()
            stored_hashed_password = user_data.get('password')

            # Check password with bcrypt
            if stored_hashed_password and bcrypt.checkpw(password.encode('utf-8'), stored_hashed_password.encode('utf-8')):
                session['username'] = username
                session['title'] = user_data.get('title', 'Candidate')
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
            session['num_questions'] = int(num_questions)
            session['questions_answered'] = 0
            session['current_difficulty_range'] = None
            logger.info(f"User '{session['username']}' started Stage 1 with {num_questions} questions.")
            return redirect('/stage1')
        except ValueError:
            logger.error(f"Invalid number of questions: {num_questions}")
            return redirect('/info')
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

    if questions_answered >= total_questions:
        logger.info(f"User '{session['username']}' has completed Stage 1.")
        return jsonify({
            "completed": True,
            "completedMessage": "You have completed Stage 1! Congratulations!"
        })

    qs_collection = db.collection('Qs')
    if current_difficulty_range:
        qs_query = qs_collection \
            .where('difficultyRange.lowerLimit', '<=', current_difficulty_range.get('rating', 0)) \
            .where('difficultyRange.upperLimit', '>=', current_difficulty_range.get('rating', 10))
    else:
        qs_query = qs_collection

    qs_docs = qs_query.get()
    if not qs_docs:
        # fallback: fetch all
        qs_docs = qs_collection.get()

    if not qs_docs:
        logger.info(f"No questions available for user '{session['username']}'.")
        return jsonify({
            "completed": True,
            "completedMessage": "No more questions available."
        })

    chosen_doc = random.choice(qs_docs).to_dict()
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
        logger.info(f"User '{session['username']}' answered question: '{question}' with rating: {rating}")
    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        return jsonify({"error": "Failed to process answer. Please try again."}), 500

    # Store the Q&A data + rating in Firestore
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
    if 'username' not in session:
        return redirect('/')
    return render_template('stage2.html')


@app.route('/get_coding_question', methods=['GET'])
def get_coding_question():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403
    try:
        qs_docs = db.collection('Coding_Qs').stream()
        questions = list(qs_docs)
        if not questions:
            logger.error("No coding questions found in database.")
            return jsonify({"error": "No questions available"}), 404

        chosen_doc = random.choice(questions).to_dict()
        session['current_coding_question'] = chosen_doc
        logger.info(f"Picked coding question for user '{session['username']}': '{chosen_doc['question']}'")
        return jsonify({"question": chosen_doc})

    except Exception as e:
        logger.error(f"Error fetching coding questions: {e}")
        return jsonify({"error": "Failed to fetch coding questions."}), 500


@app.route('/run_code', methods=['POST'])
def run_code():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    data = request.get_json()
    language = data.get('language')
    code = data.get('code')

    if not language or not code:
        logger.warning(f"User '{session['username']}' submitted incomplete code execution request.")
        return jsonify({"error": "Language and code are required."}), 400

    if language not in ['Python', 'Java']:
        logger.warning(f"User '{session['username']}' attempted unsupported language: '{language}'.")
        return jsonify({"error": "Unsupported language. Please select Python or Java."}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdirname:
            if language == 'Python':
                file_path = os.path.join(tmpdirname, 'script.py')
                with open(file_path, 'w') as f:
                    f.write(code)
                process = subprocess.run(['python3', file_path], capture_output=True, text=True, timeout=10)
            else:  # Java
                file_path = os.path.join(tmpdirname, 'Main.java')
                with open(file_path, 'w') as f:
                    f.write(code)
                compile_process = subprocess.run(['javac', file_path], capture_output=True, text=True, timeout=10)
                if compile_process.returncode != 0:
                    return jsonify({"error": f"Compilation Error:\n{compile_process.stderr}"}), 400
                class_path = tmpdirname
                process = subprocess.run(['java', '-cp', class_path, 'Main'], capture_output=True, text=True, timeout=10)

        if process.returncode != 0:
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
    if 'username' in session:
        logger.info(f"User '{session['username']}' started Stage 2: Coding Interview.")
        return redirect('/stage2')
    return redirect('/')


@app.route('/finalize_code', methods=['POST'])
def finalize_code():
    """
    1. Store user's final code in Firestore.
    2. Call Claude to generate 3 advanced conceptual questions about the code.
    3. Store these 3 questions in Firestore for Stage 3.
    """
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    data = request.get_json()
    language = data.get('language', 'Python')
    code = data.get('code', '')

    if not code.strip():
        return jsonify({"error": "No code provided."}), 400

    username = session['username']

    # 1) Store code in Firestore
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

    # 2) Call Claude for 3 advanced questions
    system_prompt = """You are an interviewer. The user has written code. Generate exactly 3 advanced conceptual questions about that code, 
focusing on edge cases, time complexity, or design. 
Return them in JSON array format, e.g.: ["Q1...","Q2...","Q3..."] 
No extra text, just JSON.
"""
    user_content = f"User's code:\n{code}"

    try:
        message = anthropic_client.messages.create(
            model="claude-3-sonnet-20240229",
            max_tokens=200,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_content}
            ]
        )
        claude_raw = message.content[0].text.strip()
        claude_questions = json.loads(claude_raw)
        if not isinstance(claude_questions, list) or len(claude_questions) != 3:
            raise ValueError("Claude did not return exactly 3 questions as a JSON list.")
        logger.info(f"Claude returned 3 questions for user '{username}'.")
    except Exception as e:
        logger.error(f"Error generating Stage 3 questions with Claude: {e}")
        return jsonify({"error": "Failed to generate Stage 3 questions."}), 500

    # 3) Store these 3 questions in Firestore
    try:
        stage3_ref = db.collection('users').document(username).collection('stage3_questions')
        stage3_ref.document("generated_questions").set({
            "questions": claude_questions,
            "timestamp": time.time()
        })
        logger.info(f"Stored stage3 questions for user '{username}'.")
    except Exception as e:
        logger.error(f"Error storing stage3 questions in Firestore: {e}")
        return jsonify({"error": "Failed to store stage3 questions."}), 500

    return jsonify({"success": True})


@app.route('/next_stage3')
def next_stage3():
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
    if 'username' not in session:
        return redirect('/')
    return render_template('stage3.html')


@app.route('/get_stage3_info', methods=['GET'])
def get_stage3_info():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 403

    username = session['username']
    stage3_doc = db.collection('users').document(username).collection('stage3_questions').document('generated_questions').get()
    if not stage3_doc.exists:
        return jsonify({"error": "No Stage 3 questions found"}), 404

    questions_data = stage3_doc.to_dict()
    questions = questions_data.get("questions", [])
    
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

    answered = session.get("stage3_answered", 0)
    total = session.get("stage3_total_qs", 0)

    if answered >= total:
        return jsonify({
            "completed": True,
            "completedMessage": "You have completed Stage 3! Congratulations!"
        })

    username = session['username']
    stage3_doc = db.collection('users').document(username).collection('stage3_questions').document('generated_questions').get()
    if not stage3_doc.exists:
        return jsonify({"error": "No Stage 3 questions found"}), 404

    questions_data = stage3_doc.to_dict()
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

    system_prompt = """You are a strict evaluator. You receive a question and expect an advanced conceptual answer.
Then you receive a user's answer. You must rate the user's answer strictly on a scale of 0 to 10.
Return ONLY the numeric rating, like "7.5" or "3.6".
"""
    user_content = f"""Question: {question}
User Answer: {user_answer}
Please provide numeric rating from 0 to 10.
"""

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
            input=synthesis_input, 
            voice=voice, 
            audio_config=audio_config
        )
        logger.info(f"Synthesized text for TTS: '{text}'")
        return response.audio_content
    except Exception as e:
        logger.error(f"Error during TTS synthesis: {e}")
        return jsonify({"error": "Failed to synthesize text."}), 500


if __name__ == '__main__':
    # For local testing only. In App Engine, gunicorn + app.yaml entrypoint is used.
    app.run(debug=True)
