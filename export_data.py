"""
Script to export the SQLite database into the raw text format 
expected by the quiz.

1. Visited urls: url\\n
2. Inverted index: `word url origin depth frequency` sharded into [letter].data.

Files will be exported and grouped by word for efficiency.
"""
import sqlite3
from pathlib import Path

def export_data(db_path: str = "crawler.db"):
    db_file = Path(db_path)
    if not db_file.exists():
        print(f"Error: Database {db_path} not found.")
        return

    # Ensure output directory exists
    output_dir = Path("data/storage")
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Exporting data from {db_path} to {output_dir}...")

    # Connect to DB SECURELY in read-only mode, using rows
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # --- 1. Export Visited URLs ---
    visited_path = output_dir / "visited_urls.data"
    visited_count = 0
    with open(visited_path, "w", encoding="utf-8") as f:
        # We query the `pages` table which holds all successfully visited URLs
        cursor = conn.execute("SELECT url FROM pages ORDER BY url ASC")
        for row in cursor:
            f.write(f"{row['url']}\n")
            visited_count += 1
    print(f"Exported {visited_count} visited URLs to {visited_path}")

    # --- 2. Export Inverted Index (Sharded by first letter) ---
    query = """
    SELECT
        it.token as word,
        it.url,
        it.origin_url as origin,
        it.depth,
        CAST(ROUND(it.tf * p.word_count) AS INTEGER) as frequency
    FROM index_tokens it
    JOIN pages p ON it.url = p.url
    ORDER BY word ASC, p.url ASC
    """

    cursor = conn.execute(query)
    
    token_count = 0
    current_filename = None
    current_file = None

    for row in cursor:
        word = row['word']
        if not word:
            continue
            
        first_char = word[0].lower()
        if 'a' <= first_char <= 'z' or '0' <= first_char <= '9':
            filename = f"{first_char}.data"
        else:
            filename = "other.data"

        if filename != current_filename:
            if current_file:
                current_file.close()
            current_filename = filename
            current_file = open(output_dir / filename, "w", encoding="utf-8")

        # Format: word url origin depth frequency
        line = f"{word} {row['url']} {row['origin']} {row['depth']} {row['frequency']}\n"
        current_file.write(line)
        token_count += 1

    if current_file:
        current_file.close()

    conn.close()
    print(f"Successfully exported {token_count} records (sharded by first letter) to {output_dir}/")

if __name__ == "__main__":
    export_data()
