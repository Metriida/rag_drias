import shutil
import warnings
from pathlib import Path
from typing import List

import torch
import transformers
import typer
import pickle
from langchain_chroma import Chroma
from langchain_core.documents.base import Document
from transformers import (
    AutoModelForCausalLM,
    AutoModelForSequenceClassification,
    AutoTokenizer,
)

from rag_drias import data
from rag_drias.crawler import crawl_website
from rag_drias.embedding import Embedding, get_embedding
from rag_drias.settings import BASE_URL, PATH_DATA, PATH_MODELS, BASE_PATH
from langchain_community.retrievers import BM25Retriever
from langchain.retrievers import EnsembleRetriever
if torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

# --- Streamlit ---

import os

import streamlit as st


def cache_resource(func):
    """Cache the resource if the function is called by a streamlit environment."""
    IS_STREAMLIT = os.getenv("IS_STREAMLIT", False)
    if IS_STREAMLIT:
        return st.cache_resource(func, ttl=3600)
    return func


app = typer.Typer(pretty_exceptions_enable=False)


# ----- Chroma Database -----


def get_db_path(
    embedding_model: str = "sentence-camembert-large", base_path: Path = BASE_PATH
) -> Path:
    """Get path of the database."""
    return base_path / "chroma_database" / embedding_model


def create_chroma_db(
    base_path: Path,
    embedding: Embedding,
    docs: List[Document],
    overwrite: bool = False,
):
    """Create a vector database from the documents"""
    path_db = get_db_path(embedding.name, base_path)
    if overwrite and path_db.exists():
        shutil.rmtree(path_db)
    path_db.mkdir(parents=True, exist_ok=True)
    if any(path_db.iterdir()):  # case overwrite = False
        raise FileExistsError(
            f"Vector database directory {path_db} is not empty. Use 'overwrite' option if needed."
        )
    vectordb = Chroma.from_documents(
        documents=docs,
        embedding=embedding,
        persist_directory=str(path_db),  # Does not accept Path
    )
    print(f"Vector database created in {path_db}")
    return vectordb


@cache_resource
def load_chroma_db(embedding_name: str, base_path: Path = BASE_PATH) -> Chroma:
    """Load the Chroma vector database."""
    path_db = get_db_path(embedding_name, base_path)
    embedding = get_embedding(embedding_name)
    if not (path_db.exists() and any(path_db.iterdir())):
        raise FileExistsError(f"Vector database {path_db} needs to be prepared.")
    return Chroma(embedding_function=embedding, persist_directory=str(path_db))

def create_bm25_idx(
    base_path: Path,
    docs: List[Document]
):
    """Create a bm25 index from the documents"""
    path_bm25 = base_path / "bm25_index.pkl"
    retriever = BM25Retriever.from_documents(docs)
    with open(path_bm25, "wb") as f:
        pickle.dump(retriever, f)

@cache_resource
def load_bm25_idx(base_path: Path = BASE_PATH) -> BM25Retriever:
    """Load the bm25 index."""
    path_bm25 = base_path / "bm25_index.pkl" 
    if not path_bm25.exists():
        raise FileExistsError(f"BM25 index {path_bm25} needs to be prepared.")
    with open(path_bm25, "rb") as f:
        retriever = pickle.load(f)
    return retriever

    
# ----- RAG -----


@cache_resource
def load_reranker(model_name: str):
    """Load the reranker model."""
    try:
        path_reranker = PATH_MODELS / model_name
        rerank_tokenizer = AutoTokenizer.from_pretrained(path_reranker)
        rerank_model = AutoModelForSequenceClassification.from_pretrained(path_reranker)
    except OSError:
        warnings.warn(
            f"\033[31mModel {model_name} not found locally. Downloading from HuggingFace.\033[0m"
        )
        rerank_tokenizer = AutoTokenizer.from_pretrained(
            model_name, trust_remote_code=True
        )
        rerank_model = AutoModelForSequenceClassification.from_pretrained(
            model_name, trust_remote_code=True
        )
    rerank_model = rerank_model.to(device)
    rerank_model.eval()
    return rerank_tokenizer, rerank_model


def rerank(
    model_name: str, text: str, docs: List[Document], k: int = 4
) -> List[Document]:
    """Returns the k most relevant chunks for the question chosen by a reranker llm."""
    rerank_tokenizer, rerank_model = load_reranker(model_name)

    rerank_inp = [[text, doc.page_content] for doc in docs]
    with torch.no_grad():
        inputs = rerank_tokenizer(
            rerank_inp,
            padding=True,
            truncation=True,
            return_tensors="pt",
            max_length=512,
        ).to(device)
        scores = (
            rerank_model(**inputs, return_dict=True)
            .logits.view(
                -1,
            )
            .float()
        )
    scores, indices = scores.topk(k)

    # map scores to float values between 0 and 1 by a sigmoid function
    scores = torch.sigmoid(scores).cpu().numpy()
    max_score = scores.max()
    # add a threshold to keep only the most relevant chunks
    indices = indices[scores > max(max_score**4, 1e-2)]

    return [docs[i] for i in indices]


def retrieve(
    text: str,
    vectordb: Chroma,
    retriever_bm25: BM25Retriever,
    n_samples: int,
    reranker: str = "",
) -> List[Document]:
    """Retrieve the most relevant chunks in relation to the query."""
    retriever_db = vectordb.as_retriever(search_kwargs={"k": n_samples})
    retriever_bm25.k = n_samples
    ensemble_retriever = EnsembleRetriever(
        retrievers=[retriever_db, retriever_bm25],
        weights=[0.7, 0.3]
        )
    chunks = ensemble_retriever.invoke(text)[:n_samples]
    # chunks = vectordb.similarity_search(text, k=n_samples)
    if reranker != "":
        chunks = rerank(reranker, text, chunks, k=n_samples // 2)
        # we return the chunks by ascending score because we get better results
        # when the relevant chunks are closer to the question
        chunks.reverse()
    return chunks


def get_prompt_message(question: str, retrieved_infos: str) -> List[dict]:
    """Get the prompt message for the LLM, with or without retrieved chunks."""
    if retrieved_infos != "":
        message = [
            {
                "role": "system",
                "content": "Le portail DRIAS (Donner accès aux scénarios climatiques Régionalisés français pour\
 l'Impact et l'Adaptation de nos Sociétés et environnement) mets à disposition les projections climatiques\
 régionalisées de référence, pour l'adaptation en France. Tu es un chatbot qui reponds aux questions à l'aide\
 d'informations récupérées sur le site.",
            },
            {
                "role": "user",
                "content": f"Avec les informations suivantes si utiles: {retrieved_infos}\nRéponds à cette question\
 de manière claire et concise: {question}\nRéponse:",
            },
        ]
    else:
        message = [
            {
                "role": "system",
                "content": "Le portail DRIAS (Donner accès aux scénarios climatiques Régionalisés français pour\
 l'Impact et l'Adaptation de nos Sociétés et environnement) mets à disposition les projections climatiques\
 régionalisées de référence, pour l'adaptation en France. Tu es un chatbot qui reponds uniquement aux questions sur le\
 site. Si une question a aucun rapport avec le site, tu dois répondre 'Je suis le Chatbot du site DRIAS, je\
 peux vous aider à comprendre et à utiliser les projections climatiques régionalisées de référence pour l'adaptation\
 en France.'.",
            },
            {
                "role": "user",
                "content": f"Réponds à cette question de manière claire et concise:\
 {question}\nRéponse:",
            },
        ]
    return message


@cache_resource
def load_llm(generative_model: str) -> tuple:
    """Load the LLM tokenizer and pipeline."""
    try:
        path_llm = PATH_MODELS / generative_model
        model = AutoModelForCausalLM.from_pretrained(
            path_llm,
            torch_dtype=torch.bfloat16,
        ).to(device)
        tokenizer = AutoTokenizer.from_pretrained(path_llm)
    except OSError:
        warnings.warn(
            f"\033[31mModel {generative_model} not found locally. Downloading from HuggingFace.\033[0m"
        )
        model = AutoModelForCausalLM.from_pretrained(
            generative_model,
            torch_dtype=torch.bfloat16,
            trust_remote_code=True,  # Allow using code that was not written by HuggingFace
        ).to(device)
        tokenizer = AutoTokenizer.from_pretrained(generative_model)
    pipeline = transformers.pipeline(
        "text-generation",
        model=model,
        tokenizer=tokenizer,
        torch_dtype=torch.bfloat16,
        device=device,
        pad_token_id=tokenizer.eos_token_id,
    )
    return tokenizer, pipeline


# ----- Typer commands -----


@app.command()
def crawl(max_depth: int = 3) -> None:
    """Crawl the Drias website and save the HTML pages."""
    PATH_DATA.mkdir(parents=True, exist_ok=True)
    print(f"Starting crawling {BASE_URL}")
    print("This may take a while...")
    crawl_website(BASE_URL, max_depth)


@app.command()
def prepare_database(
    embedding_model: str = "sentence-camembert-large",
    overwrite: bool = False,
    base_path: Path = BASE_PATH,
) -> None:
    """Prepare the Chroma vector database by chunking and embedding all the text data.

    Args:
        embedding_model (Camembert or E5): Embedding model name. Defaults to Camembert.
        overwrite (bool, optional): Whether to overwrite database. Defaults to False.
    """
    docs = data.create_docs(PATH_DATA)
    docs = data.split_to_paragraphs(docs)
    chunks = data.split_to_chunks(docs)
    embedding = get_embedding(embedding_model)
    chunks = data.filter_similar_chunks(chunks, embedding)
    create_bm25_idx(base_path, chunks)
    create_chroma_db(base_path, embedding, chunks, overwrite)


@app.command()
def query(
    text: str,
    embedding_name: str = "sentence-camembert-large",
    n_samples: int = 4,
    reranker: str = "",
    base_path: Path = BASE_PATH,
) -> List[Document]:
    """Makes a query to the vector database and retrieves the closest chunks.

    Args:
        text (str): Your query.
        embedding_name (str, optional): Embedding model name. Defaults to "Camembert".
        data_source (str, optional): Name of the data source. Defaults to "Drias".
    """
    vectordb = load_chroma_db(embedding_name, base_path)
    retriever_bm25 = load_bm25_idx(base_path)
    chunks = retrieve(text, vectordb, retriever_bm25, n_samples, reranker)
    for i, chunk in enumerate(chunks):
        print(f"---> Relevant chunk {i} <---")
        data.print_doc(chunk)
        print("-" * 20)
    return chunks


@app.command()
def answer(
    question: str,
    embedding_model: str = "sentence-camembert-large",
    generative_model: str = "Llama-3.2-3B-Instruct",
    n_samples: int = 10,
    use_rag: bool = True,
    reranker: str = "",
    base_path: Path = BASE_PATH,
    max_new_tokens: int = 500,
) -> str:
    """Generate answer to a question using RAG and print it."""

    tokenizer, pipeline = load_llm(generative_model)

    retrieved_infos = ""
    if use_rag:
        vectordb = load_chroma_db(embedding_model, base_path)
        retriever_bm25 = load_bm25_idx(base_path)
        retriever_bm25 = None
        chunks = retrieve(question, vectordb, retriever_bm25, n_samples, reranker)

        for chunk in chunks:
            retrieved_infos += f"\n-- Page Title : {chunk.metadata['title']} --\n"
            retrieved_infos += f"-- url : {chunk.metadata['url']} --\n"
            retrieved_infos += chunk.page_content

    message = get_prompt_message(question, retrieved_infos)
    prompt = tokenizer.apply_chat_template(
        message, add_generation_prompt=True, tokenize=False
    )
    print("#" * 50 + f"\nLLM input:\n{prompt}\n" + "#" * 50)

    sequences = pipeline(
        prompt,
        do_sample=True,
        temperature=0.1,
        num_return_sequences=1,
        max_new_tokens=max_new_tokens,
    )
    print(f"LLM output:\n{sequences[0]['generated_text'][len(prompt):]}")
    return sequences[0]["generated_text"][len(prompt) :]


if __name__ == "__main__":
    app()
