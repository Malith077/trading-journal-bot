import chromadb
from chromadb.utils import embedding_functions
from config import CHROMA_DB_PATH, EMBED_MODEL, RAG_COLLECTION_NAME, OLLAMA_API_URL

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
        """Indexes a single markdown file into ChromaDB."""
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Use the filename as a unique ID
        file_id = file_path.name
        self.collection.upsert(
            ids=[file_id],
            documents=[content],
            metadatas=[{"source": file_id}]
        )
        print(f"Indexed: {file_id}")

    def query_knowledge(self, query_text, n_results=3):
        """Retrieves snippets AND their source filenames."""
        results = self.collection.query(
            query_texts=[query_text],
            n_results=n_results
        )
        
        if not results['documents'] or not results['documents'][0]:
            return "", []

        # Combine the document text
        context_text = "\n---\n".join(results['documents'][0])
        
        # Extract the source filenames from metadata
        sources = [meta['source'] for meta in results['metadatas'][0]]
        
        return context_text, sources

rag_service = RAGService()