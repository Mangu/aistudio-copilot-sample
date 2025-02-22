# enable type annotation syntax on Python versions earlier than 3.9
from __future__ import annotations

import os
from openai import AzureOpenAI, AsyncAzureOpenAI
import jinja2
import pathlib

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient


from streaming_utils import add_context_to_streamed_response

templateLoader = jinja2.FileSystemLoader(pathlib.Path(__file__).parent.resolve())
templateEnv = jinja2.Environment(loader=templateLoader)
system_message_template = templateEnv.get_template("system-message.jinja2")


async def get_documents(query, num_docs=5):
    #  retrieve documents relevant to the user's question from Cognitive Search
    search_client = SearchClient(
        endpoint=os.environ["AZURE_AI_SEARCH_ENDPOINT"],
        credential=AzureKeyCredential(os.environ["AZURE_AI_SEARCH_KEY"]),
        index_name=os.environ["AZURE_AI_SEARCH_INDEX_NAME"])

    aclient = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],    
        api_version=os.environ["AZURE_OPENAI_API_VERSION"]
    )

    # generate a vector embedding of the user's question
    embedding = await aclient.embeddings.create(input=query,
                                               model=os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"])
    embedding_to_query = embedding.data[0].embedding

    context = ""
    async with search_client:
        # use the vector embedding to do a vector search on the index
        vector_query = RawVectorQuery(vector=embedding_to_query, k=num_docs, fields="contentVector")
        results = await search_client.search(
            search_text="",
            vector_queries=[vector_query],
            select=["id", "content"])

        async for result in results:
            context += f"\n>>> From: {result['id']}\n{result['content']}"

    return context


async def chat_completion(messages: list[dict], stream: bool = False,
                          session_state: any = None, context: dict[str, any] = {}):
    # get search documents for the last user message in the conversation
    user_message = messages[-1]["content"]
    documents = await get_documents(user_message, context.get("num_retrieved_docs", 5))

    # make a copy of the context and modify it with the retrieved documents
    context = dict(context)
    context['documents'] = documents

    # add retrieved documents as context to the system prompt
    system_message = system_message_template.render(context=context)
    messages.insert(0, {"role": "system", "content": system_message})

    aclient = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],    
        api_version=os.environ["AZURE_OPENAI_API_VERSION"]
    )

    # call Azure OpenAI with the system prompt and user's question
    chat_completion = await aclient.chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT"),
        messages=messages, temperature=context.get("temperature", 0.7),
        stream=stream,
        max_tokens=800)

    response = {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": chat_completion.choices[0].message.content
            },
        }]
    }

    # add context in the returned response
    if not stream:
        response["choices"][0]["context"] = context
    else:
        response = add_context_to_streamed_response(response, context)
    return response
