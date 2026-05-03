import fs from 'fs';
import path from 'path';
import { Document } from "@langchain/core/documents";
import { RecursiveCharacterTextSplitter } from "@langchain/textsplitters";
import { OpenAIEmbeddings } from "@langchain/openai";
import { Chroma } from "@langchain/community/vectorstores/chroma";
import dotenv from 'dotenv';
import { glob } from 'glob';

dotenv.config();

async function ingest() {
  const docsDir = path.join(process.cwd(), 'annuity_docs');
  console.log(`Loading TXT files from ${docsDir}...`);

  const txtFiles = glob.sync(`${docsDir}/**/*.txt`);
  console.log(`Found ${txtFiles.length} TXT files`);

  const docs = [];
  for (const file of txtFiles) {
    try {
      const text = fs.readFileSync(file, 'utf-8');
      docs.push(new Document({ pageContent: text, metadata: { source: file } }));
    } catch (e) {
      console.error(`Failed to load ${file}:`, e.message);
    }
  }

  if (docs.length === 0) {
    console.log("No documents loaded, exiting.");
    return;
  }

  console.log("Splitting documents...");
  const textSplitter = new RecursiveCharacterTextSplitter({
    chunkSize: 1000,
    chunkOverlap: 200,
  });
  const splitDocs = await textSplitter.splitDocuments(docs);
  console.log(`Split into ${splitDocs.length} chunks.`);

  console.log("Ingesting to Chroma...");
  const embeddings = new OpenAIEmbeddings({
    modelName: 'text-embedding-3-small'
  });

  await Chroma.fromDocuments(splitDocs, embeddings, {
    collectionName: "annuity_docs",
    url: "http://localhost:8000",
  });

  console.log("Ingestion complete.");
}

ingest().catch(console.error);