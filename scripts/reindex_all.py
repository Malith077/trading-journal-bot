import sys
import os
from pathlib import Path

# Ensure the script can find config and services
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config import KB_DIR, RAG_COLLECTION_NAME
from services.rag_service import rag_service

def flush_and_reindex():
    print(f"🧹 Flushing collection: {RAG_COLLECTION_NAME}...")
    
    # 1. Delete the collection to clear all old data
    try:
        rag_service.client.delete_collection(name=RAG_COLLECTION_NAME)
    except:
        print("⚠️ Collection didn't exist or already empty.")

    # 2. Re-create the collection
    rag_service.collection = rag_service.client.get_or_create_collection(
        name=RAG_COLLECTION_NAME,
        embedding_function=rag_service.embed_fn
    )

    # 3. Index all markdown files in the folder
    if KB_DIR.exists():
        files = list(KB_DIR.glob("*.md"))
        print(f"📚 Found {len(files)} articles. Starting re-index...")
        
        for file_path in files:
            rag_service.index_markdown_file(file_path)
            
        print("✅ Re-indexing complete.")
    else:
        print("⚠️ Knowledge base folder not found. Nothing to index.")

if __name__ == "__main__":
    flush_and_reindex()