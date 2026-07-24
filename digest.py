import os
import re
import time
import urllib.parse
import urllib.request
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

EXCLUDE_CHANNELS = ["announcements", "careers-and-referrals", "cards-and-rewards", "social-and-watercooler"]


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
        user_cache[user_id] = user_id
        return user_id


def resolve_mentions(text, user_cache):
    def replace_mention(match):
        uid = match.group(1)
        return f"@{resolve_username(uid, user_cache)}"
    return re.sub(r"<@([A-Z0-9]+)>", replace_mention, text)


def markdown_to_html(text):
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"^- (.+)$", r"<li>\1</li>", text, flags=re.MULTILINE)
    text = re.sub(r"(<li>.*</li>)", r"<ul>\1</ul>", text, flags=re.DOTALL)
    text = re.sub(r"^### (.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = text.replace("\n", "<br/>")
    return text


def extract_entities(text):
    # Extract stock tickers (e.g. $AAPL) and capitalised company/stock names
    tickers = re.findall(r'\$([A-Z]{1,5})\b', text)
    # Common company/stock name patterns - capitalised words that look like names
    companies = re.findall(r'\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*(?:\s(?:Inc|Ltd|Corp|Group|Holdings|Technologies|Highways|IPO))?)\b', text)
    entities = list(set(tickers + [c for c in companies if len(c) > 4]))
    return entities[:8]  # cap at 8 to avoid too many searches


def web_search(query):
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # Extract result snippets
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
        titles = re.findall(r'class="result__title"[^>]*>.*?<a[^>]*>(.*?)</a>', html, re.DOTALL)
        results = []
        for i in range(min(3, len(snippets))):
            title = re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else ""
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
            if title and snippet:
                results.append(f"- {title}: {snippet}")
        return "\n".join(results) if results else ""
    except Exception:
        return ""


def get_web_context(messages_text):
    entities = extract_entities(messages_text)
    if not entities:
        return ""
    context_parts = []
    for entity in entities:
        query = f"{entity} stock news today"
        result = web_search(query)
        if result:
            context_parts.append(f"**{entity}:**\n{result}")
        time.sleep(1)
    return "\n\n".join(context_parts)


def summarize_channel(channel_name, messages_text):
    web_context = get_web_context(messages_text)
    web_section = f"""
Current web context for stocks/companies mentioned:
{web_context}
""" if web_context else ""

    prompt = f"""You are summarizing Slack messages from the channel #{channel_name} for a busy professional who missed the last 24 hours.

Messages from the last 24 hours:
{messages_text}
{web_section}
Write a rich, detailed summary that covers:

**Gist of the Day** — 2-3 sentences capturing the overall vibe and main theme of the channel today.

**Conversation Breakdowns** — for each distinct discussion or thread:
- What was the topic or question raised, and who raised it
- What was discussed back and forth — include the key points each person made
- How it concluded or what the current status is
- Any links, resources, or recommendations shared

**Stocks & Companies** — if any stocks or companies were mentioned, provide a dedicated section with:
- What was said about each one in the channel
- Latest news or context from the web (use the web context provided above)
- Any price movements, IPOs, or news worth noting
- Do NOT skip any company or stock that was mentioned, even briefly

**Decisions & Action Items** — concrete things decided or tasks assigned, with names

**Open Questions** — anything unresolved that may need follow-up

Be specific and detailed. A reader should feel fully caught up after reading this, as if they were present in the conversation. Name people throughout. Do not skip any conversation."""

    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3000
    )
    return markdown_to_html(response.choices[0].message.content)


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

                text = resolve_mentions(text, user_cache)
                messages_text += f"[{dt}] {user}: {text}\n"

                if msg.get("reply_count", 0) > 0:
                    permalink = get_permalink(ch["id"], ts)
                    replies = get_thread_replies(ch["id"], ts)
                    thread_preview = text[:60] + "..." if len(text) > 60 else text
                    threads.append({"url": permalink, "label": f"Thread: {thread_preview}"})
                    for reply in replies[1:]:
                        reply_user = resolve_username(reply.get("user", "unknown"), user_cache)
                        reply_text = resolve_mentions(reply.get("text", ""), user_cache)
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
