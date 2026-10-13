"""Ground-truth token ids for the corpus, from the llama-server binary Ollama ships.

Ollama runs mxbai-embed-large through llama.cpp's `llama-server`, whose WPM
tokeniser is what produced every vector in conversation-memory. Ollama does not
expose token ids, but `llama-server` has `/tokenize`, so start a private one on
the same GGUF (the args match what Ollama passes):

    /Applications/Ollama.app/Contents/Resources/llama-server --model <gguf> \\
        --port 18555 --host 127.0.0.1 --no-webui --offline -c 512 -np 1 --embedding

then `python ollama_ids.py [corpus.json] [out.json]`. Each text is tokenised in
full and cut to 800 characters (what conversation-memory sends). Output stays
local: it is derived from private transcripts.
"""
import json
import sys
import urllib.request

URL = 'http://127.0.0.1:18555/tokenize'


def tokenize(text):
    req = urllib.request.Request(URL, json.dumps({'content': text, 'add_special': True}).encode(),
                                 {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req))['tokens']


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else 'corpus.json'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'corpus_ids.json'
    texts = json.load(open(src))
    out = []
    for n, t in enumerate(texts):
        out.append({'text': t, 'ids': tokenize(t), 'ids800': tokenize(t[:800])})
        if n % 500 == 0:
            print(n, flush=True)
    json.dump(out, open(dst, 'w'))
    print(len(out), 'texts ->', dst)


if __name__ == '__main__':
    main()
