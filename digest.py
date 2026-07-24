import os
import time
from datetime import datetime, timedelta, timezone
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from groq import Groq
import resend

SLACK_TOKEN = os.environ["SLACK_USER_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
TO_EMAIL = os.environ["TO_EMAIL"]
FROM_EMAIL = "onboarding@resend.dev"

slack = WebClient(token=SLACK_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)
resend.api_key = RESEND_API_KEY

EXCLUDE_CHANNELS = []  # add channel names here to exclude e.g. ["random", "general"]


def get_channels():
    channels = []
    cursor = None
    while True:
        resp = slack.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=200,
            cursor=cursor
        )
        for ch in resp["channels"]:
            if ch.get("is_member") and ch["name"] not in EXCLUDE_CHANNELS:
                channels.append({"id": ch["id"], "name": ch["name"]})
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels


def get_messages(channel_id):
    oldest = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
    messages = []
    cursor = None
    while True:
        resp = slack.conversations_history(
            channel=channel_id,
            oldest=str(oldest),
            limit=200,
            cursor=cursor
        )
        messages.extend(resp.get("messages", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor or not resp.get("has_more"):
            break
    return messages


def get_thread_replies(channel_id, thread_ts):
    resp = slack.conversations_replies(channel=channel_id, ts=thread_ts)
    return resp.get("messages", [])


def get_permalink(channel_id, message_ts):
    try:
        resp = slack.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return resp.get("permalink", "")
    except SlackApiError:
        return ""


def resolve_username(user_id, user_cache):
    if user_id in user_cache:
        return user_cache[user_id]
    try:
        resp = slack.users_info(user=user_id)
        name = resp["user"].get("real_name") or resp["user"].get("name", user_id)
        user_cache[user_id] = name
        return name
    except SlackApiError:
        return user_id


def summarize_channel(channel_name, messages_text):
    prompt = f"""You are summarizing Slack messages from the channel #{channel_name} for a daily digest.

Messages from the last 24 hours:
{messages_text}

Write a clear, structured summary with:
- Key topics discussed
- Important decisions or action items
- Notable announcements
- Any open questions

Be concise but thorough. Use bullet points."""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024
    )
    return response.choices[0].message.content


def build_email(channel_summaries):
    today = datetime.now().strftime("%A, %d %B %Y")
    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 800px; margin: auto; padding: 20px;">
    <h1 style="color: #1a1a2e;">Daily Slack Digest</h1>
    <p style="color: #666;">{today}</p>
    <hr/>
    """
    for item in channel_summaries:
        html += f"""
        <div style="margin: 30px 0; padding: 20px; border-left: 4px solid #4a90e2; background: #f9f9f9;">
            <h2 style="color: #4a90e2;">#{item['channel']}</h2>
            <p style="color: #888; font-size: 12px;">{item['message_count']} messages</p>
            <div style="white-space: pre-wrap; line-height: 1.6;">{item['summary']}</div>
            {"<h3 style='color:#333;'>Thread Links</h3>" if item['threads'] else ""}
            {"".join(f'<p><a href="{t["url"]}">{t["label"]}</a></p>' for t in item['threads'])}
        </div>
        """
    html += "</body></html>"
    return html


def main():
    print("Fetching Slack channels...")
    channels = get_channels()
    print(f"Found {len(channels)} channels")

    user_cache = {}
    channel_summaries = []

    for ch in channels:
        print(f"Processing #{ch['name']}...")
        try:
            messages = get_messages(ch["id"])
            if not messages:
                print(f"  No messages in last 24h, skipping")
                continue

            threads = []
            messages_text = ""

            for msg in messages:
                if msg.get("subtype"):
                    continue
                user = resolve_username(msg.get("user", "unknown"), user_cache)
                text = msg.get("text", "")
                ts = msg.get("ts", "")
                dt = datetime.fromtimestamp(float(ts)).strftime("%H:%M") if ts else ""

                messages_text += f"[{dt}] {user}: {text}\n"

                if msg.get("reply_count", 0) > 0:
                    permalink = get_permalink(ch["id"], ts)
                    replies = get_thread_replies(ch["id"], ts)
                    thread_preview = text[:60] + "..." if len(text) > 60 else text
                    threads.append({"url": permalink, "label": f"Thread: {thread_preview}"})
                    for reply in replies[1:]:
                        reply_user = resolve_username(reply.get("user", "unknown"), user_cache)
                        reply_text = reply.get("text", "")
                        reply_ts = reply.get("ts", "")
                        reply_dt = datetime.fromtimestamp(float(reply_ts)).strftime("%H:%M") if reply_ts else ""
                        messages_text += f"  [{reply_dt}] {reply_user} (reply): {reply_text}\n"

                time.sleep(0.5)

            summary = summarize_channel(ch["name"], messages_text)
            channel_summaries.append({
                "channel": ch["name"],
                "message_count": len(messages),
                "summary": summary,
                "threads": threads
            })

        except SlackApiError as e:
            print(f"  Error reading #{ch['name']}: {e.response['error']}")
            continue

    if not channel_summaries:
        print("No activity in last 24h — skipping email")
        return

    print(f"Sending digest for {len(channel_summaries)} active channels...")
    html = build_email(channel_summaries)
    today = datetime.now().strftime("%d %b %Y")

    resend.Emails.send({
        "from": FROM_EMAIL,
        "to": TO_EMAIL,
        "subject": f"Daily Slack Digest — {today}",
        "html": html
    })
    print("Email sent successfully!")


if __name__ == "__main__":
    main()
