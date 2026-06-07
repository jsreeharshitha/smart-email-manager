import voyageai
from config import settings
import vertexai
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel
import os

def generate_embedding(text: str, model: str = "voyage-3"):
    """
    Generates a vector embedding for the given text using Voyage AI.
    (Legacy/Primary search vector)
    """
    if not text or not text.strip():
        print(f"[*] WARNING: Skipping embedding for empty/whitespace input.")
        return [0.0] * 1024 # Standard dimension for voyage-3

    vo = voyageai.Client(api_key=settings.VOYAGE_API_KEY)
    print(f"Generating vector embedding via Voyage AI using model {model}...")
    result = vo.embed([text], model=model, input_type="document")
    return result.embeddings[0]

def generate_vertex_embedding(text: str, model_name: str = "text-embedding-004"):
    """
    Generates a 768-dimension vector embedding using Google Vertex AI.
    (Migration Target / BigQuery Search vector)
    """
    if not text or not text.strip():
        return [0.0] * 768

    try:
        project_id = os.getenv("PROJECT_ID", "grah-2026")
        vertexai.init(project=project_id, location="us-central1")
        model = TextEmbeddingModel.from_pretrained(model_name)
        
        inputs = [TextEmbeddingInput(text, "RETRIEVAL_DOCUMENT")]
        embeddings = model.get_embeddings(inputs)
        
        print(f"Generating Vertex AI embedding using model {model_name}...")
        return embeddings[0].values
    except Exception as e:
        print(f"Vertex Embedding Error: {str(e)}")
        return [0.0] * 768
