import re
import chromadb
from chromadb.utils import embedding_functions
from config import CHROMA_DB_PATH, EMBED_MODEL, RAG_COLLECTION_NAME, OLLAMA_API_URL

# --- Chunking Configuration ---
# Target ~500 tokens per chunk. Avg English word ≈ 1.3 tokens, so ~400 words.
MAX_CHUNK_CHARS = 1500
CHUNK_OVERLAP_CHARS = 200


def chunk_markdown(content: str, max_chars: int = MAX_CHUNK_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """
    Splits markdown content into overlapping chunks, respecting section boundaries.

    Strategy:
      1. Split on markdown headings (##, ###) to keep sections together.
      2. If a section exceeds max_chars, split it further on double-newlines (paragraphs).
      3. If a paragraph still exceeds max_chars, hard-split with overlap.
    """
    # Split on markdown headings (keep the heading with the content below it)
    sections = re.split(r'(?=^#{1,3}\s)', content, flags=re.MULTILINE)
    sections = [s.strip() for s in sections if s.strip()]

    chunks = []
    for section in sections:
        if len(section) <= max_chars:
            chunks.append(section)
        else:
            # Section too large — split on paragraphs (double newlines)
            paragraphs = re.split(r'\n\n+', section)
            current_chunk = ""
            for para in paragraphs:
                if len(current_chunk) + len(para) + 2 <= max_chars:
                    current_chunk = f"{current_chunk}\n\n{para}".strip()
                else:
                    if current_chunk:
                        chunks.append(current_chunk)
                    # If single paragraph exceeds max, hard-split with overlap
                    if len(para) > max_chars:
                        start = 0
                        while start < len(para):
                            end = start + max_chars
                            chunks.append(para[start:end])
                            start = end - overlap
                    else:
                        current_chunk = para
            if current_chunk:
                chunks.append(current_chunk)

    return chunks


class RAGService:
    def __init__(self):
        self.client = chromadb.PersistentClient(path=str(CHROMA_DB_PATH))
        embedding_url = f"{OLLAMA_API_URL.replace('/generate', '/embeddings')}"
        # Use Ollama for local embeddings
        self.embed_fn = embedding_functions.OllamaEmbeddingFunction(
            url=embedding_url,
            model_name=EMBED_MODEL
        )
        self.collection = self.client.get_or_create_collection(
            name=RAG_COLLECTION_NAME,
            embedding_function=self.embed_fn
        )

    def index_markdown_file(self, file_path):
        """Chunks a markdown file and indexes each chunk into ChromaDB."""
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        file_name = file_path.name
        chunks = chunk_markdown(content)

        # Remove any old chunks from this file before re-indexing
        existing = self.collection.get(where={"source": file_name})
        if existing and existing["ids"]:
            self.collection.delete(ids=existing["ids"])

        # Upsert each chunk with a unique ID
        ids = [f"{file_name}::chunk_{i}" for i in range(len(chunks))]
        metadatas = [{"source": file_name, "chunk_index": i} for i in range(len(chunks))]

        self.collection.upsert(
            ids=ids,
            documents=chunks,
            metadatas=metadatas
        )
        print(f"Indexed: {file_name} → {len(chunks)} chunk(s)")

    def query_knowledge(self, query_text, n_results=5):
        """Retrieves the most relevant chunks AND their source filenames."""
        results = self.collection.query(
            query_texts=[query_text],
            n_results=n_results
        )
        
        if not results['documents'] or not results['documents'][0]:
            return "", []

        # Combine the document text
        context_text = "\n---\n".join(results['documents'][0])
        
        # Extract unique source filenames from metadata (deduplicated, preserving order)
        sources = list(dict.fromkeys(meta['source'] for meta in results['metadatas'][0]))
        
        return context_text, sources

rag_service = RAGService()