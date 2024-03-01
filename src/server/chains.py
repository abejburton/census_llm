from langchain_openai import OpenAI, ChatOpenAI, OpenAIEmbeddings
from langchain.llms.fake import FakeListLLM
from langchain.prompts import ChatPromptTemplate, PromptTemplate
from langchain.output_parsers import RegexParser
from langchain.output_parsers.json import SimpleJsonOutputParser
from langchain.chains.question_answering import load_qa_chain
from langchain.text_splitter import CharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_core.runnables import RunnablePassthrough, Runnable, RunnableLambda

from operator import itemgetter

import json
import os
import re
from dotenv import load_dotenv
from pathlib import Path
import requests

load_dotenv()

script_path = Path(__file__).resolve()
script_dir = script_path.parent
HEAD = "http://api.census.gov/data/"

responses = ["{}"]
fake_llm = FakeListLLM(responses=responses)


def format_docs(docs):
    formatted_str = ""
    for idx, doc in enumerate(docs):
        formatted_str += f"DOCUMENT {idx+1}\nCONTENT: {doc.page_content}\n\n\n\n"
    print(formatted_str)
    return formatted_str


def get_data(file_path, key, keep):
    with open(file_path, "r") as file:
        data = json.load(file)
    datasets_to_keep = []
    for dataset in data[key]:
        if isinstance(dataset, str):
            code = dataset
            dataset = data[key][code]
            dataset["code"] = code
        dataset_strings = []
        for keep_var in keep:
            dataset_strings.append(dataset.get(keep_var, ""))
        dataset_tuple = ("---".join(dataset_strings), dataset)
        datasets_to_keep.append(dataset_tuple)
    return datasets_to_keep


def save_docembedding(embeddings_folder_path, datasets):
    data, metadata = zip(*datasets)
    splitter = CharacterTextSplitter(chunk_size=2750, chunk_overlap=0)
    xml_docs = splitter.create_documents(data, metadata)
    docembeddings = FAISS.from_documents(xml_docs, OpenAIEmbeddings())
    docembeddings.save_local(embeddings_folder_path)


class SourceChain:
    def __init__(self) -> None:
        self.template = """Split the question into three parts, the geographic region(s), 
        the general category(s) of the variables mentioned, and the relevant dataset name.
        Only use language from the question.

        If you don't know the answer, just say that you don't know, don't try to 
        make up an answer. Only use the provided context to answer.

        This should be in the following format
        Question: [question]
        Geography: [geography]
        Variable: [variable category(s)]
        Dataset: [dataset name]

        -----
        
        Question: {question}
        """
        self.prompt = ChatPromptTemplate.from_template(self.template)
        self.model = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
        self.output_parser = RegexParser(
            regex=r"Geography:(.*?)\nVariable: (.*)\nDataset:(.*)",
            output_keys=["geography", "variable", "dataset"],
        )
        self.chain = self.prompt | self.model | self.output_parser

    def invoke(self, question: str):
        return self.chain.invoke({"question": question})


class SourceRAG:
    def __init__(self) -> None:
        self.template = """
        You are a helpful Census Agent. Only use the provided metadata to answer.
        You are given the question, the identified categories, and the approximate dataset, 
        Your task is to choose the best DOCUMENT from the given list of DOCUMENTS. 
        Identify the DOCUMENT that best matches the Question, Identified Queries, and the approximate Dataset.

        Set Answer equal to a json with the keys "doc_title" and "doc_content"

        List of Datasets:
        {context}

        Question: {question}
        Identified Categories: {categories}
        Dataset: {dataset}
        Answer: """

        self.docembeddings = self.get_api_discovery_docembedding()
        self.docretriever = self.docembeddings.as_retriever(
            search_kwargs={"k": 3},
        )
        self.prompt = PromptTemplate(
            template=self.template,
            input_variables=["context", "question", "categories", "dataset"],
        )
        self.model = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
        self.output_parser = SimpleJsonOutputParser()

    def invoke(self, question, categories, dataset):
        self.chain = (
            {
                "context": itemgetter("question") | self.docretriever | format_docs,
                "question": itemgetter("question"),
                "categories": itemgetter("categories"),
                "dataset": itemgetter("dataset"),
            }
            | self.prompt
            | self.model
            | self.output_parser
        )

        self.results = self.chain.invoke(
            {
                "question": question,
                "categories": categories,
                "dataset": dataset,
            }
        )
        print(self.results)
        res_docs = self.docretriever.get_relevant_documents(self.results["doc_content"])
        self.res_doc = res_docs[0]

        return self.res_doc

    def get_api_discovery_data(self, vintage=2020):
        file_path = script_dir / Path("data/api_discovery.json")
        key = "dataset"
        keep = ["title", "description"]
        datasets = get_data(file_path, key, keep)
        datasets_to_keep = []
        for data_string, data_dict in datasets:
            if data_dict.get("c_vintage", None) == vintage:
                datasets_to_keep.append((data_string, data_dict))
        return datasets_to_keep

    def get_api_discovery_docembedding(self):
        api_discovery_path = (
            script_dir / "faiss_index" / "llm_faiss_index_api_discovery"
        )
        if not os.path.exists(api_discovery_path):
            datasets = self.get_api_discovery_data()
            save_docembedding(api_discovery_path, datasets)
        docembeddings = FAISS.load_local(api_discovery_path, OpenAIEmbeddings())
        return docembeddings


class VariableRAG:
    def __init__(self, variable_url) -> None:
        self.save_variables(variable_url)

        self.template = """
        You are a helpful Census Agent. Only use the provided metadata to answer.
        You are given the question entered by the user, the categories relevant to the question, and the dataset.
        You are also given a list of DOCUMENT. EACH DOCUMENT REPRESENTS A VARIABLE (or a VARIABLE STEM).
        Your task is to choose the best one or multiple DOCUMENT from the given list of DOCUMENT.
        Remember, each DOCUMENT could be just the partial variable.   
        Remember, each DOCUMENT could be the full variable.   
        Remember, more that one DOCUMENT could be accurate.   
        Do not give multiples if they represent the general same theme. 
        Do give multiples if they are different and add variety. 
        Identify one or multiple the DOCUMENT that best matches the Question, Identified Queries, and the Dataset.

        Set Answer equal to a json with the keys "doc_title" and "doc_content" and a value of lists.
        In case of choosing multiple DOCUMENT, set the keys "doc_title" and "doc_content" to lists.
        
        List of Variable Stems: 
        {context}

        Question: {question}
        Identified Categories: {categories}
        Dataset: {dataset}
        Answer: """

        self.prompt = PromptTemplate(
            template=self.template,
            input_variables=["context", "question", "categories", "dataset"],
        )
        self.docembedding_folder_path = self.get_variable_docembedding()
        self.model = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
        self.output_parser = SimpleJsonOutputParser()

    def save_variables(self, variable_url):
        self.url = variable_url
        self.file_path = Path(
            script_dir / "data" / self.url.replace(HEAD, "").replace("/", "_")
        )
        if not os.path.exists(self.file_path):
            resp = requests.get(self.url)
            with open(self.file_path, "wb") as file:
                file.write(resp.content)

    def invoke(self, question, categories, dataset):

        key = "root"
        self.docretriever = FAISS.load_local(
            self.docembedding_folder_path / key, OpenAIEmbeddings()
        ).as_retriever(
            search_kwargs={"k": 20},
        )
        while True:
            self.chain = (
                {
                    "context": itemgetter("question") | self.docretriever | format_docs,
                    "question": itemgetter("question"),
                    "categories": itemgetter("categories"),
                    "dataset": itemgetter("dataset"),
                }
                | self.prompt
                | self.model
                | self.output_parser
            )
            self.results = self.chain.invoke(
                {
                    "question": question,
                    "categories": categories,
                    "dataset": dataset,
                }
            )
            print(self.results)
            print("~!~!~!")
            if isinstance(self.results["doc_content"], list):
                k = self.results["doc_content"][0]
            else:
                k = self.results["doc_content"]
            self.docretriever = FAISS.load_local(
                self.docembedding_folder_path / k,
                OpenAIEmbeddings(),
            ).as_retriever(
                search_kwargs={"k": 20},
            )
            a = self.docretriever.get_relevant_documents(question)
            if len(a) == 1:
                break
            else:
                print("continue")
                continue

        return self.results

    def get_variable_data(self):
        key = "variables"
        keep = ["label", "concept"]
        datasets = get_data(self.file_path, key, keep)
        v = VarTree()
        for data, metadata in datasets:
            branch = metadata["label"].strip().replace(":", "")
            branch = re.sub("^!!", "", branch).split("!!")
            v.append(branch, (data, metadata))
        return v

    def get_variable_docembedding(self):
        docembedding_folder_path = (
            script_dir
            / "faiss_index"
            / f"llm_faiss_index_{self.url.replace(HEAD, '').replace('/', '_').replace('.json', '')}_folder"
        )
        if not os.path.exists(docembedding_folder_path):
            var_tree = self.get_variable_data()
            level = "root"
            save_variable_embedding(docembedding_folder_path, level, var_tree)
        return docembedding_folder_path


def save_variable_embedding(docembedding_folder_path, level, v):
    datasets = []
    metadatas = []
    if len(v.children.keys()) == 0:
        datasets = [v.dataset[0]]
        metadatasets = [v.dataset[1]]
    else:
        for key, child in v.children.items():
            save_variable_embedding(docembedding_folder_path, key, child)
            metadata = {}
            metadata["key"] = key
            cur_dataset = []
            cur_dataset.append(key)
            if v.dataset is not None:
                metadata["metadata"] = v.dataset[1]
                cur_dataset.append(v.dataset[0])
            dataset = "---".join(cur_dataset)
            datasets.append(dataset)
            metadatas.append(metadata)

    splitter = CharacterTextSplitter(chunk_size=2750, chunk_overlap=0)
    docs = splitter.create_documents(datasets, metadatas)
    docembeddings = FAISS.from_documents(docs, OpenAIEmbeddings())
    docembeddings.save_local(docembedding_folder_path / level)


class VarTree:
    def __init__(self) -> None:
        self.children = {}
        self.dataset = None

    def append(self, branch, label_dataset):
        if len(branch) == 0:
            self.dataset = label_dataset
        elif branch[0] in self.children.keys():
            self.children[branch[0]].append(branch[1:], label_dataset)
        else:
            v = VarTree()
            v.append(branch[1:], label_dataset)
            self.children[branch[0]] = v
