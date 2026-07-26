# Ralph Loop v3 🤖

Deterministyczny orkiestrator Codex do implementowania całych modułów na podstawie
PRD. Model wykonuje pracę twórczą, ale o przejściach stanu, zakresie zmian,
testach i finalizacji decyduje kod orkiestratora.

## Przepływ

```text
📋 kontrakt zadania
   ↓
🧠 Planner ──→ walidacja wyniku JSON i zakresu zmian
   ↓
🛠️  Implementer ──→ 🧪 deterministyczne quality gates
   ↑                         ↓
   └──── poprawki ←── 🔍 niezależny Reviewer
                              ↓ PASS
                   🧪 finalne quality gates
                              ↓
                   📦 commit → 🔗 fast-forward
```

Każde zadanie działa na osobnej gałęzi i w osobnym Git worktree. Główna gałąź
jest przesuwana dopiero po `PASS`, ponownym przejściu wszystkich bramek i
potwierdzonym commicie. Wyniki ról, pełne logi i append-only journal trafiają do
`.ralph/runtime/` i nie są commitowane.

## Start

Wymagania: Python 3.9+, Git oraz zalogowany Codex CLI.

```bash
./ralph init --prd docs/my-module-prd.md \
  --manifest docs/tasks/my-module/manifest.json

# Uzupełnij PRD, manifest i task.md, a potem zacommituj stan początkowy.
./ralph doctor
./ralph status
./ralph run
```

Instalacja jako polecenie systemowe jest opcjonalna:

```bash
python3 -m pip install -e .
ralph run
```

## Kontrakt zadania

Kompletny przykład jest w [`examples/manifest.json`](examples/manifest.json), a
szablon opisu zadania w [`examples/task.md`](examples/task.md).

Każde zadanie musi mieć:

- stabilne ID i zależności;
- kryteria `AC-*` z jednoznacznym opisem;
- `contract.allowedPaths`, czyli technicznie egzekwowany zakres implementera;
- quality gates jako tablice argumentów, np. `["npm", "test"]` — bez `sh -c` i
  bez wyciągania skryptów z Markdowna;
- osobne limity prób dla planowania, implementacji i review;
- katalog z `task.md`.

Statusy tworzą jawną maszynę stanów:

```text
ready → planning → planned → implementing → in_review → completed
            ↑              ↖ needs_changes ↙       │
            └──────── needs_replan ─────────────────┘
```

`blocked` i `failed` zatrzymują sesję bez scalania częściowego kodu. Gałąź i
worktree pozostają wtedy dostępne do inspekcji, a terminal pokazuje ich nazwę.

## Determinizm i bezpieczeństwo

- Wynik każdej roli przechodzi przez JSON Schema oraz dodatkową walidację
  kompletności dowodów dla wszystkich `AC-*`.
- Planner może zmienić tylko `plan.md`, reviewer tylko `review.md`, a implementer
  jedynie `progress.md` oraz ścieżki z kontraktu.
- Niedozwolona zmiana natychmiast zatrzymuje workflow.
- Quality gates są uruchamiane bez powłoki, z timeoutem oraz stałymi `TZ`, locale
  i `PYTHONHASHSEED`.
- Sieć agenta jest domyślnie wyłączona. Można ją jawnie włączyć w
  `.ralph/config.json`, jeżeli zadanie rzeczywiście jej wymaga.
- Przed review i po `PASS` wykonywany jest ten sam zestaw bramek.
- Journal pozwala odtworzyć commit po przerwaniu procesu między commitem a
  fast-forwardem.
- Główny worktree musi być czysty i nie może zmienić HEAD podczas pracy zadania.

## Czysty terminal ✨

Standardowy widok pokazuje wyłącznie etapy, wynik, czas oraz odnośnik do logu w
razie błędu. Surowy output Codex i testów jest przechowywany w runtime zamiast
zalewać terminal. `./ralph run --verbose` dodaje stan i liczniki prób, a
`--color never` wyłącza ANSI (emoji pozostają).

## Recovery

Ponowne `./ralph run` automatycznie używa aktywnego worktree. Jeśli proces padł
po utworzeniu commita, ale przed fast-forwardem, Ralph wykrywa commit po SHA i
kończy transakcję. Przy blokadzie nic nie jest scalane do głównej gałęzi.

## Testy Ralpha

```bash
python3 -m unittest discover -s tests -v
```

Test end-to-end używa fałszywych agentów i prawdziwych worktree/commitów, dzięki
czemu pilnuje również samej „fabryki aplikacji”, nie tylko generowanego kodu.
