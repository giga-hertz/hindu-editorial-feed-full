import os
import sys
import time
import re
import urllib.parse
from datetime import datetime
import email.utils
import json
import xml.dom.minidom as minidom
import requests
from bs4 import BeautifulSoup

RSS_SOURCE_URL = "https://www.thehindu.com/opinion/editorial/feeder/default.rss"
SMRY_API_ENDPOINT = "https://r.smry.ai/api/v1/read"
OUTPUT_FILE = "feed.xml"
MAX_ITEMS = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def clean_url(url):
    """Strips query parameters and fragments for stable GUIDs."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))

def get_text_content(element):
    """Safely extracts text or CDATA node contents."""
    return "".join(
        c.nodeValue for c in element.childNodes 
        if c.nodeType in (c.TEXT_NODE, c.CDATA_SECTION_NODE)
    ).strip()

def get_item_identifier(item):
    """Extracts unique GUID or link from an existing item."""
    for tag in ("guid", "link"):
        nodes = item.getElementsByTagName(tag)
        if nodes:
            val = get_text_content(nodes[0])
            if val:
                return clean_url(val)
    return None

def fetch_rss_feed_items():
    """Fetches the official The Hindu Editorial RSS feed and parses its items."""
    try:
        resp = requests.get(RSS_SOURCE_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"Error fetching Hindu RSS feed: {e}", file=sys.stderr)
        return []

    dom = minidom.parseString(resp.content)
    raw_items = dom.getElementsByTagName("item")
    parsed_items = []

    for item in raw_items:
        title = ""
        link = ""
        desc = ""
        pub_date = ""

        title_nodes = item.getElementsByTagName("title")
        if title_nodes:
            title = get_text_content(title_nodes[0])

        link_nodes = item.getElementsByTagName("link")
        if link_nodes:
            link = get_text_content(link_nodes[0])

        desc_nodes = item.getElementsByTagName("description")
        if desc_nodes:
            desc = get_text_content(desc_nodes[0])

        date_nodes = item.getElementsByTagName("pubDate")
        if date_nodes:
            pub_date = get_text_content(date_nodes[0])
        else:
            pub_date = email.utils.formatdate(usegmt=True)

        if link and title:
            parsed_items.append({
                "title": title,
                "link": clean_url(link),
                "rss_summary": desc,
                "pub_date": pub_date
            })

    return parsed_items

def clean_and_format_text(paragraphs):
    """Converts a list of article paragraph strings into theme-adaptive HTML."""
    html_out = []
    for text in paragraphs:
        text = text.strip()
        if not text:
            continue

        # Remove bracketed links like [text](url) or [Also Read: ...]
        text = re.sub(r'#{0,6}\s*!?\[[^\]]*\]\s*:?\s*(?:\([^\)]+\)|<[^>]+>|https?://\S+)', '', text)
        text = re.sub(r'\[\s*\](?:\([^\)]*\)|<[^>]+>)?', '', text)

        # Convert markdown bold/italics
        text = re.sub(r'\*\*\s*([\s\S]+?)\s*\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'__\s*([\s\S]+?)\s*__', r'<strong>\1</strong>', text)
        text = re.sub(r'(?<!\*)\*(?!\*)\s*([^\*\n]+?)\s*(?<!\*)\*(?!\*)', r'<em>\1</em>', text)

        # Bullet point detection
        if re.match(r'^[-*•–—]\s+', text):
            item_text = re.sub(r'^[-*•–—]\s*', '', text)
            html_out.append(
                f'<p style="margin: 0 0 1.25em 1.5em; text-indent: -1.2em; line-height: 1.85; font-size: 1.05em;">'
                f'<span>- </span>{item_text}'
                f'</p>'
            )
            continue

        html_out.append(f'<p style="margin: 0 0 1.25em 0; line-height: 1.85; font-size: 1.05em;">{text}</p>')

    return "".join(html_out)

def fetch_full_article_content(article_url):
    """
    Extracts full article text using a 3-stage strategy:
    1. Direct HTML inspection of The Hindu article body selectors.
    2. Embedded JSON-LD Schema (articleBody).
    3. SMRY Reader API fallback.
    """
    paragraphs = []
    image_url = ""
    summary = ""

    # Strategy 1 & 2: Direct Page Fetch & JSON-LD
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract og:image
            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                image_url = og_img["content"].strip()

            # Extract og:description
            og_desc = soup.find("meta", property="og:description")
            if og_desc and og_desc.get("content"):
                summary = og_desc["content"].strip()

            # Attempt finding article containers
            body_containers = soup.select(
                "div.articlebodycontent, [id^='content-body-'], div.article-body, div.article__content-body"
            )

            if body_containers:
                for p in body_containers[0].find_all("p"):
                    # Avoid ads, related news, and subscription promos
                    text = p.get_text().strip()
                    parent_classes = " ".join(p.parent.get("class", []))
                    if any(term in parent_classes.lower() for term in ("ad-", "promo", "paywall", "subscribe", "related")):
                        continue
                    if len(text) > 25 and not any(skip in text.lower() for skip in ("also read", "click here", "subscribe to read")):
                        paragraphs.append(text)

            # Check JSON-LD if direct HTML was paywalled / truncated
            if len(paragraphs) < 2:
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        data = json.loads(script.string)
                        if isinstance(data, list):
                            data = data[0]
                        if isinstance(data, dict) and "articleBody" in data:
                            raw_body = data["articleBody"]
                            body_paras = [b.strip() for b in raw_body.split("\n\n") if len(b.strip()) > 25]
                            if body_paras:
                                paragraphs = body_paras
                                break
                    except Exception:
                        pass
    except Exception as e:
        print(f"Direct fetch failed for {article_url}: {e}")

    # Strategy 3: SMRY Reader Fallback if direct content is too short
    if len(paragraphs) < 2:
        print(f"Triggering SMRY reader fallback for: {article_url}")
        time.sleep(3.5)
        try:
            smry_resp = requests.get(
                SMRY_API_ENDPOINT, 
                params={"url": article_url}, 
                headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}, 
                timeout=20
            )
            if smry_resp.status_code == 202:
                time.sleep(4)
                smry_resp = requests.get(
                    SMRY_API_ENDPOINT, 
                    params={"url": article_url}, 
                    headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}, 
                    timeout=20
                )
            if smry_resp.status_code == 200:
                data = smry_resp.json()
                content = data.get("content") or data.get("text") or data.get("markdown") or ""
                if isinstance(content, str) and len(content.strip()) > 100:
                    paragraphs = [p.strip() for p in content.split("\n\n") if len(p.strip()) > 20]
        except Exception as e:
            print(f"SMRY fallback error: {e}")

    body_html = clean_and_format_text(paragraphs)
    return {
        "body_html": body_html,
        "image_url": image_url,
        "summary": summary
    }

def create_item_node(dom, item_data, content_details):
    """Creates a standard RSS 2.0 item node with media enclosure and rich content."""
    item = dom.createElement("item")

    # Title
    title_elem = dom.createElement("title")
    title_elem.appendChild(dom.createCDATASection(item_data["title"]))
    item.appendChild(title_elem)

    # Link & Guid
    clean_article_url = item_data["link"]
    link_elem = dom.createElement("link")
    link_elem.appendChild(dom.createTextNode(clean_article_url))
    item.appendChild(link_elem)

    guid_elem = dom.createElement("guid")
    guid_elem.setAttribute("isPermaLink", "true")
    guid_elem.appendChild(dom.createTextNode(clean_article_url))
    item.appendChild(guid_elem)

    # Creator
    author_elem = dom.createElement("dc:creator")
    author_elem.appendChild(dom.createCDATASection("The Hindu Editorial"))
    item.appendChild(author_elem)

    # PubDate
    pub_elem = dom.createElement("pubDate")
    pub_elem.appendChild(dom.createTextNode(item_data["pub_date"]))
    item.appendChild(pub_elem)

    # Category
    cat_elem = dom.createElement("category")
    cat_elem.appendChild(dom.createCDATASection("Editorial"))
    item.appendChild(cat_elem)

    # Image Enclosure
    image_url = content_details.get("image_url")
    if image_url:
        enclosure = dom.createElement("enclosure")
        enclosure.setAttribute("url", image_url)
        enclosure.setAttribute("type", "image/jpeg")
        enclosure.setAttribute("length", "0")
        item.appendChild(enclosure)

    # Build rich HTML body
    summary_text = content_details.get("summary") or item_data.get("rss_summary") or ""
    body_html = content_details.get("body_html") or ""

    full_html = ""
    if image_url:
        full_html += f'<p style="margin-bottom: 1.25em;"><img src="{image_url}" style="max-width:100%; height:auto; border-radius: 6px;" /></p>'
    if summary_text:
        full_html += f'<p style="margin-bottom: 1.25em; font-size: 1.1em; line-height: 1.6;"><strong>{summary_text}</strong></p><hr style="border:0; border-top: 1px solid rgba(128,128,128,0.25); margin: 1.5em 0;"/>'
    if body_html:
        full_html += body_html
    else:
        full_html += f'<p style="margin: 0 0 1.25em 0; line-height: 1.85;">{summary_text}</p>'
    full_html += f'<p style="margin-top: 2em; padding-top: 1em; border-top: 1px solid rgba(128,128,128,0.25);"><a href="{clean_article_url}" target="_blank">📰 Read Original on The Hindu</a></p>'

    # Summary teaser in <description>
    desc_elem = dom.createElement("description")
    desc_elem.appendChild(dom.createCDATASection(f"<p>{summary_text}</p>"))
    item.appendChild(desc_elem)

    # Full HTML in <content:encoded>
    content_encoded_elem = dom.createElement("content:encoded")
    content_encoded_elem.appendChild(dom.createCDATASection(full_html))
    item.appendChild(content_encoded_elem)

    return item

def initialize_empty_feed():
    """Builds a fresh RSS 2.0 DOM document."""
    impl = minidom.getDOMImplementation()
    dom = impl.createDocument(None, "rss", None)
    rss = dom.documentElement
    rss.setAttribute("version", "2.0")
    rss.setAttribute("xmlns:content", "http://purl.org/rss/1.0/modules/content/")
    rss.setAttribute("xmlns:dc", "http://purl.org/dc/elements/1.1/")

    channel = dom.createElement("channel")
    rss.appendChild(channel)

    title = dom.createElement("title")
    title.appendChild(dom.createTextNode("The Hindu - Full Text Editorial Feed"))
    channel.appendChild(title)

    link = dom.createElement("link")
    link.appendChild(dom.createTextNode("https://www.thehindu.com/opinion/editorial/"))
    channel.appendChild(link)

    desc = dom.createElement("description")
    desc.appendChild(dom.createTextNode("Daily Editorials from The Hindu with full article contents."))
    channel.appendChild(desc)

    return dom, channel

def main():
    # Load or initialize feed.xml
    if os.path.exists(OUTPUT_FILE) and os.path.getsize(OUTPUT_FILE) > 0:
        try:
            dom = minidom.parse(OUTPUT_FILE)
            channel = dom.getElementsByTagName("channel")[0]
        except Exception:
            dom, channel = initialize_empty_feed()
    else:
        dom, channel = initialize_empty_feed()

    existing_items = list(channel.getElementsByTagName("item"))
    existing_ids = {get_item_identifier(it) for it in existing_items if get_item_identifier(it)}

    # Fetch new items from the Hindu RSS feeder
    incoming_items = fetch_rss_feed_items()
    print(f"Total articles in source RSS: {len(incoming_items)}")

    new_articles = [it for it in incoming_items if clean_url(it["link"]) not in existing_ids]
    print(f"New articles to process: {len(new_articles)}")

    if not new_articles:
        print("No new articles found. Exiting.")
        return

    new_item_nodes = []
    for item_data in new_articles:
        print(f"Fetching full text for: {item_data['title']}")
        content_details = fetch_full_article_content(item_data["link"])
        item_node = create_item_node(dom, item_data, content_details)
        new_item_nodes.append(item_node)
        time.sleep(1.5)  # Polite delay between requests

    # Prepend newest articles to top
    first_existing = existing_items[0] if existing_items else None
    for item_node in new_item_nodes:
        if first_existing:
            channel.insertBefore(item_node, first_existing)
            channel.insertBefore(dom.createTextNode("\n    "), first_existing)
        else:
            channel.appendChild(dom.createTextNode("\n    "))
            channel.appendChild(item_node)

    # Enforce rolling limit of 100 items
    all_items = list(channel.getElementsByTagName("item"))
    if len(all_items) > MAX_ITEMS:
        for excess in all_items[MAX_ITEMS:]:
            prev = excess.previousSibling
            if prev and prev.nodeType == prev.TEXT_NODE and prev.nodeValue.strip() == "":
                channel.removeChild(prev)
            channel.removeChild(excess)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(dom.toxml(encoding="utf-8"))

    total = len(channel.getElementsByTagName("item"))
    print(f"Successfully processed {len(new_item_nodes)} new articles. Total in feed: {total}")

if __name__ == "__main__":
    main()
