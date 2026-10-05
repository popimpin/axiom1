#!/bin/bash
# Rerun every number in docs/MEASUREMENTS.md's final section with the current code, on Nemotron (Nebius).
# One command: bash scripts/rerun_all.sh [OUT_DIR]       (needs NEBIUS_API_KEY; about $2 and 3-4 hours)
#
#   1. unit tests                    5. AgentDojo prompt injection (5 attack styles x 6 goals x 16 vectors)
#   2. 50 scored real threads        6. five whole Enron mailboxes (resumable: rerun to continue)
#   3. the generated inbox           7. the page data (web/timeline/data) from these results
#   4. the calendar connector demo
# A step that fails prints "STOP: <step>" and ends the run; nothing later runs on top of a failure.
set -u
cd "$(dirname "$0")/.."
OUT="${1:-docs/measurements/final}"
mkdir -p "$OUT/mailboxes"
export PYTHONIOENCODING=utf-8
unset NEBIUS_BASE_URL AXIOM_THINKING_SWITCH
: "${NEBIUS_API_KEY:?set NEBIUS_API_KEY}"
NANO=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B
SUPER=nvidia/nemotron-3-super-120b-a12b
step() { echo "== $(date +%H:%M:%S) $*"; }
stop() { echo "STOP: $*"; exit 1; }

step "1 unit tests"
python -m unittest discover -s tests > "$OUT/tests.log" 2>&1
tail -3 "$OUT/tests.log" | grep -q "^OK" || stop "unit tests (see $OUT/tests.log)"
grep "^Ran" "$OUT/tests.log"

step "2 scored real threads (MailEx, 50)"
python examples/mailex_eval.py --model $NANO --agree $NANO $SUPER --out "$OUT/mailex.json" > "$OUT/mailex.log" 2>&1
grep SCORE "$OUT/mailex.log" || stop "mailex"

step "3 generated inbox (10 inboxes)"
python examples/real_inbox.py run --inboxes 10 --seed 1 --model $NANO --agree $NANO $SUPER --out "$OUT/generated.json" > "$OUT/generated.log" 2>&1 \
  || stop "generated inbox"
python -c "import json; r=json.load(open('$OUT/generated.json'))['rows']; print('GENERATED', sum(x['label']=='witnessed' for x in r), '/', len(r))"

step "4 calendar connector demo"
python examples/calendar_demo.py --receipt "$OUT/generated.json" --out "$OUT/calendar_demo.json" || stop "calendar demo"

step "5 AgentDojo prompt injection"
python examples/agentdojo_eval.py --out "$OUT/agentdojo.json" > "$OUT/agentdojo.log" 2>&1
grep SUMMARY "$OUT/agentdojo.log" || stop "agentdojo"

step "6 whole mailboxes"
for b in heard-m giron-d steffes-j haedicke-m lay-k; do
  python examples/mailbox_run.py --box $b --model $NANO --agree $NANO $SUPER --out "$OUT/mailboxes" > "$OUT/mailbox_$b.log" 2>&1
  grep SUMMARY "$OUT/mailbox_$b.log" || stop "mailbox $b"
  grep -q "PIPELINE ERROR" "$OUT/mailbox_$b.log" && stop "pipeline errors in $b"
done

step "7 page data"
AXIOM_BOXES_DIR="$OUT/mailboxes" AXIOM_MAILEX_RECEIPT="$OUT/mailex.json" python examples/timeline_sets.py | tail -8 || stop "page data"
echo "ALL DONE"
