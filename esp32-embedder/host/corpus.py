"""Sample real message texts from local Claude Code transcripts for tokeniser testing (stays local)."""
import glob, json, random, sys
from pathlib import Path
random.seed(42)
files = glob.glob(str(Path.home() / '.claude' / 'projects' / '*' / '*.jsonl'))
texts = []
for f in random.sample(files, min(len(files), 400)):
    try:
        for line in open(f, errors='replace'):
            try:
                m = json.loads(line).get('message') or {}
            except Exception:
                continue
            c = m.get('content')
            parts = [c] if isinstance(c, str) else [p.get('text') for p in (c or []) if isinstance(p, dict) and p.get('type') == 'text']
            for p in parts:
                if p and p.strip():
                    texts.append(p.strip()[:2000])
    except Exception:
        pass
random.shuffle(texts)
texts = list(dict.fromkeys(texts))[:int(sys.argv[1]) if len(sys.argv) > 1 else 3000]
json.dump(texts, open('corpus.json', 'w'))
print(len(files), 'files ->', len(texts), 'texts; non-ASCII texts:', sum(any(ord(ch) > 127 for ch in t) for t in texts))
