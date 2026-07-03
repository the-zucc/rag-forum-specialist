#!/usr/bin/env python3
"""RAG pipeline using LangChain, Ollama, and OpenSearch."""

from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.vectorstores import OpenSearchVectorSearch
from langchain_community.llms import Ollama
from langchain_classic.chains import RetrievalQA
from langchain_core.prompts import PromptTemplate


def initialize_rag(
    opensearch_url: str = "http://localhost:9200",
    index_name: str = "forum-posts",
    ollama_base_url: str = "http://localhost:11434",
    embed_model: str = "nomic-embed-text",
    llm_model: str = "llama2",
):
    """Initialize the RAG pipeline with OpenSearch and Ollama.

    Args:
        opensearch_url: URL of the OpenSearch instance
        index_name: Name of the OpenSearch index to query
        ollama_base_url: Base URL for Ollama
        embed_model: Embedding model name in Ollama
        llm_model: LLM model name in Ollama

    Returns:
        A RetrievalQA chain ready to answer questions
    """
    embeddings = OllamaEmbeddings(
        base_url=ollama_base_url,
        model=embed_model,
    )

    vector_store = OpenSearchVectorSearch(
        opensearch_url=opensearch_url,
        index_name=index_name,
        embedding_function=embeddings,
    )

    llm = Ollama(
        base_url=ollama_base_url,
        model=llm_model,
        temperature=0.3,
    )

    prompt_template = PromptTemplate(
        input_variables=["context", "question"],
        template="""Use the following pieces of context to answer the question.
If you don't know the answer from the context, say so.

Context:
{context}

Question: {question}

Answer:""",
    )

    qa_chain = RetrievalQA.from_chain_type(
        llm=llm,
        chain_type="stuff",
        retriever=vector_store.as_retriever(
            search_kwargs={"k": 5, "text_field": "body_text", "metadata_field": "*"}
        ),
        chain_type_kwargs={"prompt": prompt_template},
        return_source_documents=True,
    )

    return qa_chain


def ask_rag(qa_chain, question: str) -> dict:
    """Ask a question to the RAG pipeline.

    Args:
        qa_chain: The initialized RAG chain
        question: The user's question

    Returns:
        A dict with 'answer' and 'sources' keys
    """
    result = qa_chain({"query": question})

    sources = []
    if "source_documents" in result:
        sources = [
            {
                "content": doc.page_content,
                "metadata": doc.metadata,
            }
            for doc in result["source_documents"]
        ]

    return {
        "answer": result.get("result", ""),
        "sources": sources,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RAG pipeline for forum posts")
    parser.add_argument("question", help="Question to ask the RAG system")
    parser.add_argument(
        "--opensearch-url",
        default="http://localhost:9200",
        help="OpenSearch URL",
    )
    parser.add_argument(
        "--index-name",
        default="forum-posts",
        help="OpenSearch index name",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama base URL",
    )
    parser.add_argument(
        "--embed-model",
        default="nomic-embed-text",
        help="Embedding model in Ollama",
    )
    parser.add_argument(
        "--llm-model",
        default="llama2",
        help="LLM model in Ollama",
    )

    args = parser.parse_args()

    print("Initializing RAG pipeline...")
    qa_chain = initialize_rag(
        opensearch_url=args.opensearch_url,
        index_name=args.index_name,
        ollama_base_url=args.ollama_url,
        embed_model=args.embed_model,
        llm_model=args.llm_model,
    )

    print(f"\nQuestion: {args.question}\n")
    result = ask_rag(qa_chain, args.question)

    print(f"Answer:\n{result['answer']}\n")

    if result["sources"]:
        print("Sources:")
        for i, source in enumerate(result["sources"], 1):
            print(f"\n{i}. {source['metadata']}")
            print(f"   {source['content'][:200]}...")
