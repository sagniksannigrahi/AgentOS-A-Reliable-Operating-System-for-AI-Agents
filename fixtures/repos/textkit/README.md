# Textkit: small text utilities (slugify, word counting, and a report built on both)

* `text.py` – `slugify(s)`: lowercase, words joined by `-`, non-alphanumerics removed.
* `counts.py` – `word_count(s)`: number of whitespace-separated words.
* `report.py` – `summary(s)`: returns `"<slug> (<n> words)"` using the two functions above.
