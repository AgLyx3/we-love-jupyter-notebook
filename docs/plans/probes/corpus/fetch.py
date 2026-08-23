"""Download the corpus's remote notebooks at their pinned commits.

Referenced, not vendored. Third-party notebooks stay out of this repo — we do
not have redistribution rights for all of them — but a pinned sha is the same
bytes forever, so the corpus is still reproducible.

Run: python3 docs/plans/probes/corpus/fetch.py
     python3 docs/plans/probes/corpus/fetch.py --force
"""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).parent
CACHE = HERE / ".cache"
RAW = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"


def fetch(entry: dict, force: bool) -> pathlib.Path:
    dest = CACHE / f"{entry['id']}.ipynb"
    if dest.exists() and not force:
        return dest
    url = RAW.format(repo=entry["repo"], sha=entry["sha"],
                     path=urllib.parse.quote(entry["path"]))
    with urllib.request.urlopen(url, timeout=180) as response:
        payload = response.read()
    json.loads(payload)  # fail here rather than three scripts downstream
    CACHE.mkdir(exist_ok=True)
    dest.write_bytes(payload)
    return dest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-download cached notebooks")
    args = parser.parse_args()

    corpus = json.loads((HERE / "corpus.json").read_text())
    (CACHE / ".gitignore").parent.mkdir(exist_ok=True)
    (CACHE / ".gitignore").write_text("*\n")

    for entry in corpus["remote"]:
        try:
            path = fetch(entry, args.force)
        except Exception as error:  # network, 404, moved repo
            print(f"  !  {entry['id']}: {type(error).__name__}: {error}")
            continue
        cells = len(json.loads(path.read_text())["cells"])
        print(f"  ok {entry['id']:24s} {cells:4d} cells  {entry['repo']}@{entry['sha'][:7]}")


if __name__ == "__main__":
    main()
