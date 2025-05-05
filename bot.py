# bot.py
import ollama
import os
import json
import logging
from datetime import datetime
from collections import Counter
from bson import ObjectId
import requests

from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama.llms import OllamaLLM

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    # Removed ReactionTypeEmoji as we are not using message_reaction updates
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler, # We'll use this for the button click
    ConversationHandler,
    filters,
    ContextTypes,
    # Removed filters import again - already present in the main block
)

from db import db
from scoring import compute_city_scores
import pandas as pd

# load .env, including BOT_TOKEN, MONGO_URI, WEBAPP_BASE_URL
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Conversation states (only for setup)
OWNER_CRITERIA, = range(1)

# Criteria options (label, field)
CRITERIA_OPTIONS = [
    ("Adult Nightlife", "adult_nightlife"),
    ("Happiness Level", "happiness_level"),
    ("Entertainment Options",  "fun"),
    ("Walkability", "walkability"),
    ("Friendly to Foreigners", "friendly_to_foreigners"),
    ("Budget", "budget"),
]

# Your Web App host (HTTPS) serving vote.html
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL")  # e.g. https://abcd.ngrok.io
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434") # Default O-llama URL
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3") # Default O-llama model
SKYSK_API_KEY = 'sh967490139224896692439644109194' # Placeholder - replace with actual key


# --- Configuration for Button Join ---
JOIN_BUTTON_EMOJI = "✅" # Emoji for the button label
JOIN_CALLBACK_PATTERN = "^join_session\|" # Pattern for the callback data


def main():
    app = ApplicationBuilder().token(os.getenv("BOT_TOKEN")).build()

    # --- Owner: setup criteria in group ---
    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("setup_criteria", setup_criteria)],
        states={
            OWNER_CRITERIA: [
                CallbackQueryHandler(handle_criteria_toggle,
                                     pattern="^(toggle|done)\|?.*")
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_setup)],
    )
    app.add_handler(setup_conv)

    # --- Group: start voting (now invites users who clicked join button) ---
    # Removed the /join command handler
    app.add_handler(CommandHandler("start_voting", start_voting))

    # --- New: Handler for join button clicks ---
    app.add_handler(CallbackQueryHandler(
        handle_join_button_click, # Our new handler function
        pattern=JOIN_CALLBACK_PATTERN # Listen for callbacks starting with our pattern
    ))


    # --- Private chat: /vote → open Web App button ---
    app.add_handler(
        CommandHandler(
            "vote",
            start_member_vote,
            filters=filters.ChatType.PRIVATE
        )
    )
    # Handle the form submission from the Web App (remains the same)
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.WEB_APP_DATA,
            handle_webapp_data
        )
    )

    # --- Group: close votes & Tinder round (remains the same) ---
    app.add_handler(CommandHandler("close_votes", close_votes))


    # --- New: Group command to get city description (remains the same) ---
    app.add_handler(CommandHandler("description", get_city_description, filters=filters.ChatType.GROUPS))


    # start polling
    app.bot.delete_webhook(drop_pending_updates=True)
    app.run_polling(drop_pending_updates=True)


# === Group Handlers ===

async def setup_criteria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: choose which criteria will be voted on, then post message with join button."""
    context.chat_data["owner_id"] = update.effective_user.id
    # inline toggles
    keyboard = [
        [InlineKeyboardButton(f"[ ] {label}", callback_data=f"toggle|{label}")]
        for label, _ in CRITERIA_OPTIONS
    ]
    keyboard.append([InlineKeyboardButton("Done", callback_data="done")])
    context.chat_data["new_criteria"] = []
    await update.message.reply_text(
        "📋 *Select criteria* (tap to toggle):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return OWNER_CRITERIA


async def handle_criteria_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Answer the callback query immediately

    data = query.data

    sel = context.chat_data.get("new_criteria", [])
    if data.startswith("toggle|"):
        label = data.split("|", 1)[1]
        if label in sel:
            sel.remove(label)
        else:
            sel.append(label)
        context.chat_data["new_criteria"] = sel

        # rebuild keyboard
        keyboard = []
        for lbl, _ in CRITERIA_OPTIONS:
            mark = "✓" if lbl in sel else " "
            keyboard.append(
                [InlineKeyboardButton(f"[{mark}] {lbl}", callback_data=f"toggle|{lbl}")]
            )
        keyboard.append([InlineKeyboardButton("Done", callback_data="done")])
        await query.edit_message_reply_markup( # Edit the message's keyboard
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return OWNER_CRITERIA

    # finalize setup and send the join message with button
    selected = context.chat_data.get("new_criteria", [])
    if not selected:
        await query.edit_message_text("You must select at least one criterion.") # Edit message instead of new reply
        return OWNER_CRITERIA # Stay in conversation if no criteria selected

    fields = [f for lbl, f in CRITERIA_OPTIONS if lbl in selected]

    # Create the session document
    session_data = {
        "owner_id":      update.effective_user.id,
        "chat_id":       update.effective_chat.id,
        "criteria_list": fields,
        "votes":         [],
        "pending":       [],       # user_ids who clicked the join button
        "status":        "open",
        "created_at":    datetime.utcnow(),
        # No need to store message_id for join button using this approach
    }
    insert_result = db.criteria_sessions.insert_one(session_data)
    session_id = insert_result.inserted_id # Get the ID of the new session

    # Build the join button with callback data including the session ID
    join_button = InlineKeyboardButton(
        text=f"{JOIN_BUTTON_EMOJI} Join Voting Session",
        callback_data=f"join_session|{session_id}" # Data to identify action and session
    )
    join_keyboard = InlineKeyboardMarkup([[join_button]])

    # Send the message that users will click the button on
    await query.edit_message_text( # Edit the setup message to show confirmation and the join prompt
        f"✅ Voting session for *{', '.join(selected)}* started!\n\nClick the button below to join the session and receive the voting form via DM.",
        parse_mode="Markdown",
        reply_markup=join_keyboard # Attach the join button
    )

    # Clean up chat_data from setup
    context.chat_data.pop("owner_id", None)
    context.chat_data.pop("new_criteria", None)

    return ConversationHandler.END


# Removed the empty handle_join_reaction function

async def handle_join_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles clicks on the join voting session button."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    query_data = query.data # e.g., "join_session|60b8d8f2f8a4c7b..."

    # Answer the query immediately to remove the loading state from the button
    await query.answer("Joining...") # Optional message shown to the user

    # Extract session ID from callback data
    try:
        # Split "join_session|session_id"
        _, sid_str = query_data.split("|", 1)
        sess_id = ObjectId(sid_str)
    except ValueError:
        logging.error(f"Malformed callback data received: {query_data}")
        await query.edit_message_text("❌ Error processing your join request.") # Edit the message on error
        return
    except Exception as e:
        logging.error(f"Error extracting session ID from callback data {query_data}: {e}")
        await query.edit_message_text("❌ Error processing your join request.") # Edit the message on error
        return


    # Find the active session corresponding to the button clicked
    sess = db.criteria_sessions.find_one({
        "_id": sess_id, # Find by session ID from callback
        "chat_id": chat_id, # Ensure it's in the same chat
        "status":  "open"
    })

    if not sess:
        # Session not found, not open, or button clicked in wrong chat
        await query.edit_message_text("⚠️ This session is no longer active or you clicked in the wrong chat.") # Edit the message
        logging.warning(f"Join button clicked for inactive/not found session {sess_id} by user {user_id} in chat {chat_id}")
        return

    # User clicked the join button, register them if not already pending
    if user_id not in sess.get("pending", []):
        db.criteria_sessions.update_one(
            {"_id": sess_id},
            {"$push": {"pending": user_id}}
        )
        logging.info(f"User {user_id} registered for session {sess_id} via button click.")

        # Optional: Send a confirmation message to the user in private chat
        # We need the user's display name (from the query object) and chat title (from message object)
        user_name = query.from_user.full_name
        chat_title = query.message.chat.title
        try:
             await context.bot.send_message(
                chat_id=user_id, # Send to the user's private chat
                text=f"✅ You've been registered for the voting session in *{chat_title}*! Look out for the /vote command invitation from me.",
                parse_mode="Markdown"
             )
        except Exception as e:
            logging.warning(f"Failed to send registration confirmation DM to user {user_id}: {e}")

        # Optional: Edit the button/message to show the user they've joined
        # You could fetch the updated session to get the count of pending users and update the button text
        # For simplicity now, we'll just answer the query. Editing the message might require fetching it again.
        # If you want to edit, consider the logic when multiple users click the button.
        # A simple edit could be: await query.edit_message_reply_markup(reply_markup=None) # Remove button after click
        pass # Keep the message/button as is for now, maybe add a check in the UI later

    else:
        # User was already pending
        logging.debug(f"User {user_id} already pending for session {sess_id}.")
        await query.answer("You're already registered.") # Send a different message to the user

async def start_voting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: DM all joined (pending) users an invite to /vote in private."""
    sess = db.criteria_sessions.find_one({
        "chat_id":  update.effective_chat.id,
        "owner_id": update.effective_user.id,
        "status":   "open"
    })
    if not sess:
        return await update.message.reply_text("No session to start.")

    pending_users = sess.get("pending", [])
    if not pending_users:
         return await update.message.reply_text(f"No one has joined the session yet! Tell people to click the '{JOIN_BUTTON_EMOJI} Join Voting Session' button above.") # Update message

    await update.message.reply_text(f"🚀 Starting voting round for {len(pending_users)} participants...")

    for uid in pending_users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="👋 Hi! Please send /vote in this chat to open the vote form for the current session."
            )
            logging.debug(f"Sent /vote invite to user {uid}.")
        except Exception as e:
            # Log the error if DM fails (user might have blocked bot)
            logging.warning(f"Failed to DM user {uid} to start vote: {e}")

    await update.message.reply_text("✅ Invitations sent via DM to registered users.")


# === Private Chat Handlers ===

async def start_member_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """In private: send a WebApp button to open the HTML form."""
    # Find the open session where the user is in the pending list
    # Or has already voted in an open session
    sess = db.criteria_sessions.find_one({
        "$or": [
            {"pending": update.effective_user.id},
            {"votes.user_id": update.effective_user.id}
        ],
        "status":  "open"
    })

    if not sess:
        return await update.message.reply_text(
            f"❌ You are not registered for or have not voted in any open session. Find a group session and click the '{JOIN_BUTTON_EMOJI} Join Voting Session' button first!" # Update message
        )

    # Check specifically if they have already voted in *this* session
    user_has_voted = any(v["user_id"] == update.effective_user.id for v in sess.get("votes", []))

    if user_has_voted:
         return await update.message.reply_text("You have already voted for the current session.")


    # Build and log the WebApp URL
    url = f"{WEBAPP_BASE_URL}/vote?session_id={sess['_id']}"
    logging.info("🔗 WebApp URL = %s", url)

    button = InlineKeyboardButton(
        text="📝 Open Vote Form",
        web_app=WebAppInfo(url=url)
    )
    kb = InlineKeyboardMarkup([[button]])
    await update.message.reply_text(
        "Click to open the voting form:",
        reply_markup=kb
    )


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified handler for vote submissions and Tinder swipes."""
    msg = update.effective_message
    webapp = getattr(msg, "web_app_data", None)
    if not webapp or not webapp.data:
        logging.warning("Received non-WebApp data update in handle_webapp_data.")
        return  # not a WebAppData update

    # 1) Parse JSON payload
    try:
        data = json.loads(webapp.data)
    except json.JSONDecodeError:
        logging.error("Malformed WebApp data received: %s", webapp.data)
        return await msg.reply_text("❌ Malformed data.")

    logging.info("WebApp payload: %s", data)

    # 2) Handle vote form submission (has 'weights')
    if "weights" in data:
        sid_str = data.get("session_id")
        name    = data.get("name")
        weights = data.get("weights")

        if not sid_str or not name or not isinstance(weights, dict):
            logging.error("Invalid vote data structure: %s", data)
            return await msg.reply_text("❌ Invalid vote data.")

        try:
            sid = ObjectId(sid_str)
        except:
            logging.error("Invalid session identifier in vote data: %s", sid_str)
            return await msg.reply_text("❌ Invalid session identifier.")

        # Check if user has already voted for this session
        existing_vote_sess = db.criteria_sessions.find_one({
            "_id": sid,
            "votes.user_id": msg.from_user.id
        })
        if existing_vote_sess:
             logging.warning(f"User {msg.from_user.id} attempted to vote again for session {sid_str}. Overwriting previous vote.")
             # Remove existing vote
             db.criteria_sessions.update_one(
                 {"_id": sid},
                 {"$pull": {"votes": {"user_id": msg.from_user.id}}}
             )

        # Add the new vote
        db.criteria_sessions.update_one(
            {"_id": sid},
            {
                "$push": {"votes": {
                    "user_id": msg.from_user.id,
                    "name":    name,
                    "weights": weights
                }}
            },
            upsert=False
        )
        logging.info(f"Added/Overwrote vote for user {msg.from_user.id} in session {sid_str}.")

        # Always remove user from pending list after a successful submission
        db.criteria_sessions.update_one(
            {"_id": sid},
            {"$pull": {"pending": msg.from_user.id}}
        )
        logging.info(f"Removed user {msg.from_user.id} from pending list for session {sid_str}.")


        return await msg.reply_text("✅ Your vote has been recorded. Thanks!")


    # 4) Handle completion signal (now includes all swipes)
    if data.get("complete"):
        sid_str = data.get("session_id")
        collected_swipes = data.get("swipes", {}) # Expect 'swipes' key with collected data

        if not sid_str:
             logging.error("Missing session_id in complete signal payload: %s", data)
             # No reply needed as WebApp is likely closing
             return

        try:
            sid = ObjectId(sid_str)
        except Exception as e:
            logging.error("Invalid session identifier in complete signal payload: %s | Error: %s", sid_str, e)
            return # Invalid ID, nothing more to do

        logging.info(f"Received complete signal and collected swipes for session_id={sid_str}, user={msg.from_user.id}")

        # --- Process the collected swipes batch ---
        if isinstance(collected_swipes, dict): # Ensure 'swipes' is a dictionary
            logging.info(f"Processing {len(collected_swipes)} collected swipes.")

            # Find the tinder session associated with the criteria session ID
            tinder_sess = db.tinder_sessions.find_one({"session_id": sid})
            if not tinder_sess:
                logging.warning(f"Tinder session not found for criteria session ID: {sid_str}. Cannot record swipes.")
                 # Optionally inform the user
                await msg.reply_text("❌ Error: Could not find the associated swipe session.")
                return

            tinder_sess_id = tinder_sess["_id"]

            # Before pushing new votes, remove any previous votes from this user in this tinder session
            db.tinder_sessions.update_one(
                {"_id": tinder_sess_id},
                {"$pull": {"yes_votes": {"user_id": msg.from_user.id}}}
            )
            db.tinder_sessions.update_one(
                {"_id": tinder_sess_id},
                {"$pull": {"no_votes": {"user_id": msg.from_user.id}}}
            )
            logging.debug(f"Removed previous swipe votes for user {msg.from_user.id} in Tinder session {tinder_sess_id}.")


            for city, choice in collected_swipes.items():
                # Validate city and choice format within the collected data
                if isinstance(city, str) and choice in ['yes', 'no']:
                     try:
                        # Push each recorded swipe to the database
                        db.tinder_sessions.update_one(
                            {"_id": tinder_sess_id}, # Use the Tinder session ID
                            {"$push": {f"{choice}_votes": {
                                "user_id": msg.from_user.id,
                                "city": city
                            }}},
                            upsert=False # Ensure the document exists
                        )
                        # Log each recorded swipe from the collected data
                        logging.debug(f"Recorded collected swipe: city='{city}', choice='{choice}' for user {msg.from_user.id} in Tinder session {tinder_sess_id}")
                     except Exception as e:
                        logging.error(f"Error recording collected swipe '{city}'/'{choice}' for user {msg.from_user.id} in Tinder session {tinder_sess_id}: {e}")
                else:
                     logging.warning(f"Skipping invalid collected swipe data format: city='{city}', choice='{choice}' for user {msg.from_user.id}")
        else:
            logging.warning(f"Expected 'swipes' to be a dict but got {type(collected_swipes)} in complete payload for session {sid_str}. Data: %s", data)

        # --- Rest of the original complete handler logic (calculate winner, send message, mark finished) ---
        # Find the Tinder session again (needed to get yes_votes etc. after updates)
        ts = db.tinder_sessions.find_one({"session_id": sid}) # Still use criteria session ID to find Tinder sess
        if not ts:
            logging.warning(f"Tinder Session not found after processing collected swipes: {sid_str}")
            await msg.reply_text("❌ Error finding swipe results session.")
            return

        yes_list = [v["city"] for v in ts.get("yes_votes", []) if isinstance(v, dict) and "city" in v] # Added safety checks

        result = "" # Initialize result message
        if yes_list:
            # Calculate votes per city using Counter
            vote_counts = Counter(yes_list)
            most_common_cities = vote_counts.most_common()

            result_lines = ["🏆 Team's Top City(s):"]
            if most_common_cities:
                max_count = most_common_cities[0][1]
                # Find all cities that tied for the max count
                tied_cities = [city_name for city_name, count in most_common_cities if count == max_count]

                if len(tied_cities) > 1:
                     result_lines.append(f"It's a tie! The top cities are:")
                     for city_name in tied_cities:
                          # Find the total yes votes for this city across all users
                          total_yes_votes = sum(1 for v in ts.get("yes_votes", []) if isinstance(v, dict) and v.get("city") == city_name)
                          result_lines.append(f"- *{city_name}* ({total_yes_votes} votes)")
                else:
                    # Single winner
                    winner_city = most_common_cities[0][0]
                    winner_count = most_common_cities[0][1]
                    result_lines = [f"🏆 The team’s top city is *{winner_city}* with {winner_count} votes!"] # Simpler message for single winner


                result = "\n".join(result_lines)
            else:
                 # This else might be redundant if yes_list is empty, but good safety
                 result = "⚠️ No YES votes recorded for this session."

        else:
            result = "⚠️ No YES votes were cast for any city."


        # Send the result back into the original group chat where the session was started
        crit_sess = db.criteria_sessions.find_one({"_id": ts.get("session_id")}) # Use .get for safety
        if crit_sess and "chat_id" in crit_sess:
             try:
                 await context.bot.send_message(
                    chat_id=crit_sess["chat_id"],
                    text=result,
                    parse_mode="Markdown"
                 )
                 logging.info(f"Sent final result message to chat {crit_sess['chat_id']} for session {sid_str}.")
             except Exception as e:
                 logging.error(f"Failed to send final message to chat {crit_sess.get('chat_id')} for session {sid_str}: {e}")
                 # Optionally inform the user via a reply message that results couldn't be posted
                 await msg.reply_text("Finished, but couldn't post results to the group chat.")

        else:
             logging.warning(f"Could not find original chat_id for session {sid_str} to post results.")
             await msg.reply_text("Finished, but could not determine where to post results.")


        # Mark Tinder session as finished in DB
        # Note: The criteria session is already marked 'closed' by /close_votes
        db.tinder_sessions.update_one(
            {"_id": ts["_id"]}, # Mark the Tinder session finished
            {"$set": {"status": "finished"}}
        )
        logging.info(f"Marked Tinder session {ts['_id']} as finished.")

        return # This handler finishes

    # 5) Unexpected payload
    logging.warning("Unrecognized WebApp payload received in handle_webapp_data: %s", data)
    return await msg.reply_text("❌ Unrecognized WebApp payload.")


# === Group: Close & Tinder Round ===

async def close_votes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: Closes the voting session, computes scores, and starts Tinder round."""

    sess = db.criteria_sessions.find_one({
        "chat_id":  update.effective_chat.id,
        "owner_id": update.effective_user.id,
        "status":   "open"
    })

    if not sess:
        return await update.message.reply_text("❌ No open session to close.")

    votes = sess.get("votes", [])
    if not votes:
        return await update.message.reply_text(
            "❌ No votes have been recorded yet. Make sure everyone has submitted their form (/vote via DM)."
        )

    # Get the selected criteria fields for this session
    criteria_fields = sess.get("criteria_list", [])
    if not criteria_fields:
         logging.error(f"Session {sess['_id']} has no criteria_list.")
         return await update.message.reply_text("❌ Error: Voting criteria not found for this session.")

    # Prepare group preferences for the scoring function
    # This needs to be a list of lists, where each inner list corresponds to a user's ratings
    # for the criteria in the order specified by `criteria_fields`.
    user_preferences_for_scoring = []
    for vote in votes:
        user_weights = vote.get("weights", {})
        # Create a list of scores for the selected criteria in the correct order
        # Use a default value (e.g., "3") if a user somehow missed a criterion in their submission
        # Convert values to string as compute_city_scores expects strings
        user_pref_row = [str(user_weights.get(field, "3")) for field in criteria_fields]
        user_preferences_for_scoring.append(user_pref_row)

    if not user_preferences_for_scoring:
         logging.error(f"Could not prepare user preferences for scoring for session {sess['_id']}.")
         return await update.message.reply_text("❌ Error: Could not prepare user preferences for scoring.")

    # Close the criteria voting session
    db.criteria_sessions.update_one(
        {"_id": sess["_id"]},
        {"$set": {"status": "closed", "closed_at": datetime.utcnow()}}
    )
    await update.message.reply_text("🔒 Votes closed. Computing top cities based on your preferences…")

    # --- Call the scoring function ---
    # NOTE: The group_origins, group_budgets, departure_date, return_date
    # are placeholders. You need to implement a way to collect these from users
    # if they are required for your actual flight filtering/scoring logic.
    # Also, ensure SKYSK_API_KEY is set in your environment.
    if not SKYSK_API_KEY:
        logging.error("SKYSK_API_KEY environment variable not set. Flight filtering may fail.")
        await update.message.reply_text("⚠️ Warning: Flight API key not set. Flight filtering may not work.")


    # Placeholder values - REPLACE WITH ACTUAL LOGIC
    group_origins = ["LON"]
    # Process budget criteria from votes if available
    all_budgets = [
        int(vote["weights"].get("budget", 1500)) # Get budget from weights, default to 1500 if missing
        for vote in votes if "weights" in vote and "budget" in vote["weights"]
    ]
    # Simple example: use min budget voted as the max budget for flight search
    max_group_budget = min(all_budgets) if all_budgets else 4000 # Default max budget if no budget votes
    logging.info(f"Using inferred max group budget for flights: {max_group_budget}")
    # The scoring function expects a list of budget dicts, let's send a range based on the min voted budget
    # You might need more sophisticated budget handling depending on your scoring logic
    group_budgets_for_scoring = [{"min": 0, "max": max_group_budget}] # Sending a range


    departure_date = "2025-08-01" # Placeholder - replace with actual date logic
    return_date = "2025-08-15" # Placeholder - replace with actual date logic


    logging.info(f"Calling compute_city_scores with {len(user_preferences_for_scoring)} users, criteria: {criteria_fields}, budget range: {group_budgets_for_scoring}")

    # Pass relevant parameters to compute_city_scores
    try:
        top5_df = compute_city_scores(
            group_preferences=user_preferences_for_scoring,
            maximum_group_budgets=all_budgets, # Using processed budgets
        )
        logging.info(f"compute_city_scores returned DataFrame with {len(top5_df)} rows.")
         # Log the full top5_df for debugging
        logging.debug("Top Cities DataFrame:\n%s", top5_df.to_string())


    except Exception as e:
        logging.error(f"Error occurred during compute_city_scores: {e}", exc_info=True) # Log traceback
        await update.message.reply_text(f"❌ An error occurred while computing city scores: {e}")
        # Optionally reopen the session or mark it as errored if scoring failed
        db.criteria_sessions.update_one(
            {"_id": sess["_id"]},
            {"$set": {"status": "scoring_failed"}}
        )
        return


    # Extract city names from the resulting DataFrame for the Tinder round
    if top5_df.empty:
         top5_cities = []
         await update.message.reply_text("⚠️ Scoring did not find any suitable cities based on preferences and filters.")
         # Optionally don't start the Tinder round if no cities are found
         db.criteria_sessions.update_one(
            {"_id": sess["_id"]},
            {"$set": {"status": "no_cities_found_after_scoring"}} # Specific status
         )
         return
    else:
        # Get the 'city' column as a list
        top5_cities = top5_df['city'].tolist()
        logging.info(f"Top cities computed: {top5_cities}")


    # --- Start the Tinder Round ---
    # Create a new Tinder session document linked to the criteria session
    tid = db.tinder_sessions.insert_one({
        "session_id": sess["_id"], # Link back to the criteria session
        "suggestions": top5_cities, # Use the list of top city names
        "yes_votes": [],
        "no_votes": [],
        "status": "voting",
        # Only users who *successfully voted* criteria can participate in Tinder round
        "participants": [v["user_id"] for v in sess["votes"]]
    }).inserted_id
    logging.info(f"Created Tinder session {tid} for criteria session {sess['_id']}.")

    # Send WebApp swipe button to participants in private chat
    # Use the criteria session ID for the WebApp URL as the frontend expects it
    sid_str = str(sess["_id"])
    url = f"{WEBAPP_BASE_URL}/tinder?session_id={sid_str}"
    logging.info("Tinder WebApp URL: %s", url)

    button = InlineKeyboardButton(
        text="🏙️ Swipe Cities",
        web_app=WebAppInfo(url=url),
    )
    kb = InlineKeyboardMarkup([[button]])

    participants_ids = db.tinder_sessions.find_one({"_id": tid}).get("participants", [])
    if not participants_ids:
         logging.warning(f"No participants found for Tinder session {tid}.")
         await update.message.reply_text("⚠️ No participants registered for the Tinder round (no one voted criteria).")
         # Set Tinder session status to finished if no participants
         db.tinder_sessions.update_one({"_id": tid}, {"$set": {"status": "finished_no_participants"}})
         return


    logging.info(f"Sending Tinder WebApp invitation to {len(participants_ids)} participants.")
    invited_count = 0
    for uid in participants_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"🏙️ The top {len(top5_cities)} cities have been computed based on your votes! Swipe through them to choose your favorites:",
                parse_mode="Markdown", # Ensure markdown works for bold cities if needed in the future
                reply_markup=kb
            )
            logging.debug(f"Sent Tinder invite to user {uid}.")
            invited_count += 1
        except Exception as e:
            logging.warning(f"Couldn't DM user {uid} for Tinder invite: {e}")

    # Notify the group that swipe-invitations have been sent
    await update.message.reply_text(
        f"✅ Votes closed and scoring complete. Invitations to swipe the {len(top5_cities)} suggested cities have been sent via DM to {invited_count} participants."
    )

# === New Command Handler for Description ===
async def get_city_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group command to get a touristic description of the most recent winning city."""
    chat_id = update.effective_chat.id
    logging.info(f"Received /description command in chat {chat_id}")

    # await update.message.reply_text("Fetching information about the winning city...")

    # Find the most recent finished Tinder session for this group's criteria session
    # First, find the most recent criteria session in this chat (assuming the winning city is based on the *most recent* process)
    recent_criteria_sess = db.criteria_sessions.find_one(
        {"chat_id": chat_id},
        sort=[("created_at", -1)]
    )

    if not recent_criteria_sess:
        await update.message.reply_text("❌ No voting sessions found in this group.")
        return

    # Then, find the Tinder session linked to this criteria session that is finished
    tinder_sess = db.tinder_sessions.find_one(
        {"session_id": recent_criteria_sess["_id"], "status": "finished"},
        sort=[("_id", -1)] # Get the most recent finished Tinder session if multiple exist for one crit session
    )

    if not tinder_sess:
        await update.message.reply_text("⚠️ No finished swipe session found for the last criteria session where a winning city was determined.")
        return

    yes_list = [v["city"] for v in tinder_sess.get("yes_votes", []) if isinstance(v, dict) and "city" in v]

    if not yes_list:
        await update.message.reply_text("⚠️ The last finished swipe session had no 'Yes' votes recorded.")
        return

    # Calculate the winning city (or the top one in case of a tie)
    vote_counts = Counter(yes_list)
    most_common_cities = vote_counts.most_common()

    if not most_common_cities:
        await update.message.reply_text("⚠️ Could not determine a winning city from the last swipe session's votes.")
        return

    # Get the top city (or the first one in case of a tie)
    winning_city = most_common_cities[0][0]
    max_votes = most_common_cities[0][1]

    # Check for ties and mention them
    tied_cities = [city for city, count in most_common_cities if count == max_votes]
    if len(tied_cities) > 1:
        # We'll provide description for the first city in the tie list
        # No need to send a separate "Fetching description..." message, it's already sent above
        winning_city_for_description = tied_cities[0]
        # await update.message.reply_text(f"Based on the last session, *{winning_city_for_description}* tied for the top spot with {max_votes} votes. Fetching description...", parse_mode="Markdown")
    else:
         winning_city_for_description = winning_city # Use the single winner
        #  await update.message.reply_text(f"Based on the last session, the top city is *{winning_city_for_description}* with {max_votes} votes. Fetching description...", parse_mode="Markdown")




    API_KEY = Global_API  # Reemplaza con tu API key real

    # Coordenadas de ejemplo (París)
    # Load cities data from CSV (ensure the CSV has 'city', 'latitude', and 'longitude' columns)
    cities = pd.read_csv("cities.csv")

    # Find the row matching the winning city (case-insensitive)
    match = cities[cities['city'].str.lower() == winning_city_for_description.lower()]

    if not match.empty:
        lat = float(match.iloc[0]['latitude'])
        lon = float(match.iloc[0]['longitude'])
    else:
        # Fallback coordinates (e.g., Paris) if the city is not found in the CSV
        lat = 48.8566
        lon = 2.3522
    radius = 1000  # en meters

    # Endpoint base
    url = "https://api.opentripmap.com/0.1/en/places/radius"

    # Parámetros de la petición
    type_place = ["accomodations", "restaurants", "interesting_places"]
    places = []
    for i in type_place:
        params = {
            "apikey": API_KEY,
            "lat": lat,
            "lon": lon,
            "radius": radius,
            "limit": 5,  # max resultats 
            "kinds": i,  # type of place
        }

        # make the request
        response = requests.get(url, params=params)

        # Verificar estado y mostrar resultados
        if response.status_code == 200:
            print("Correct")
        else:
            print("Error:", response.status_code, response.text)
        
        content = response.json()
        places.append(content)

    # # --- Call O-llama API for description ---
    # ollama_description = ""
    # # Ensure OLLAMA_API_URL is set
    # if not OLLAMA_API_URL:
    #      logging.error("OLLAMA_API_URL environment variable not set. Cannot fetch city description.")
    #      await update.message.reply_text("❌ Error: O-llama API URL not configured. Cannot fetch city description.")
    #      return

    # Construct the prompt for O-llama
    # template = "Question {question}" \
    # "Answer: Discover with me the city of {city}." 
    # prompt = ChatPromptTemplate.from_template(template, city=winning_city_for_description)
    # logging.info(f"Calling O-llama API for description of {winning_city_for_description}")

    try:
        # Construct the request payload for O-llama API
        # payload = {
        #     "model": OLLAMA_MODEL,
        #     "prompt": prompt,
        #     "stream": False # We need the full response at once
        # }
        # headers = {'Content-Type': 'application/json'}

        # # Make the POST request to the O-llama API
        # response = requests.post(OLLAMA_API_URL, headers=headers, data=json.dumps(payload))
        # response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)

        # # Parse the JSON response
        # result = response.json()
        # # Extract the generated text (this might vary slightly based on O-llama API version/model)
         # Use the O-llama chat function
        model = OllamaLLM(
            model="gemma3",
            base_url='http://localhost:11434', # O-llama API URL
        )

        ollama_description = model.invoke(information(places))

    except requests.exceptions.RequestException as e:
        logging.error(f"Error calling O-llama API for {winning_city_for_description}: {e}", exc_info=True)
        ollama_description = f"Failed to fetch description for {winning_city_for_description}. API error: {e}. Is O-llama running?" # Error message

    # Send the description back to the group chat
    if ollama_description:
        await update.message.reply_text(
            f"Here's a little something about *{winning_city_for_description}*:\n\n{ollama_description}",
            parse_mode=None
        )
    else:
         # Should not happen if ollama_description is set to an error message on failure, but as fallback:
        await update.message.reply_text(f"Could not get description for {winning_city_for_description}.")


def information(json_data):
    # Extraer información relevante
    system_prompt = f"""
    Analyze the following JSON data and extract the relevant information about the places:
    {json.dumps(json_data, indent=4, ensure_ascii=False)}
    
    Follow the following rules:
    1.  Identify the name of the place.
    2.  Identify the type of place (e.g., restaurant, hotel, etc.).
    3.  Identify the distace in meters from the center of the city. 
    4.  Provide the rating of the place.
    5.  Give as reference the osm and wikidata when possible.
    6.  Provide an ordered list of the places based on the type of place.
    7.  Generate a summary of the places in a human-readable format.
    8.  Use different phrasings and keywords to get diverse results.
    9.  You are recomandator system so be friendly and engaging. 
    10. Do not talk about the source of the information, JSON or code, just give the information about the place.
    """

    return system_prompt






async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels criteria setup conversation."""
    await update.message.reply_text("❌ Setup canceled.")
    # Clean up any potential partial data in chat_data if needed
    context.chat_data.pop("owner_id", None)
    context.chat_data.pop("new_criteria", None)
    return ConversationHandler.END # Make sure this function returns END




if __name__ == "__main__":
    main()