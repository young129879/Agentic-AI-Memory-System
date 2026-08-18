# Memory System - Quick Reference

**Last Updated:** October 10, 2025  
**Phase 2 Complete:** Retrieval & Metadata Extraction ✅

---

## 🚀 Quick Start Integration

```python
# Add to main.py imports
from memory.retrieval.crawler import LatticeCrawler
from memory.retrieval.intent_analyzer import IntentAnalyzer
from memory.metadata_extractor import MetadataExtractor, MEMORY_SYSTEM_PROMPT

# Initialize (in main() function)
crawler = LatticeCrawler(storage, max_days_back=7)
intent_analyzer = IntentAnalyzer()
metadata_extractor = MetadataExtractor(fallback_to_simple=True)
```

---

## 🔍 Usage Pattern (Full Flow)

```python
# 1. Analyze intent
intent = intent_analyzer.analyze(user_query)

# 2. Retrieve context
today = datetime.now().strftime("%Y-%m-%d")
context = crawler.retrieve_context(intent, today, max_results=5)

# 3. Build enhanced prompt
prompt = MEMORY_SYSTEM_PROMPT + "\n\n"
if context.contexts:
    prompt += "[MEMORY CONTEXT]:\n"
    for ctx in context.contexts[:3]:
        prompt += f"- {ctx['day_id']}: {ctx.get('context', '')[:100]}\n"
prompt += f"\nUser: {user_query}"

# 4. Call LLM
full_response = llm.chat(prompt)

# 5. Parse response
user_reply, metadata = metadata_extractor.parse_response(full_response)

# 6. Show user clean reply (metadata hidden)
print(f"Assistant: {user_reply}")

# 7. Save turn with metadata
conversation_mgr.log_turn(session_id, user_query, user_reply, metadata['keywords'])
```

---

## 📊 API Reference

### IntentAnalyzer
```python
analyzer = IntentAnalyzer()
intent = analyzer.analyze(query)

# Returns Intent:
#   - keywords: List[str]
#   - query_type: QueryType (CHAT, MEMORY_QUERY, TASK_REQUEST, TASK_UPDATE)
#   - confidence: float (0.0-1.0)
```

### LatticeCrawler
```python
crawler = LatticeCrawler(storage, max_days_back=7)
context = crawler.retrieve_context(intent, day_id, max_results=5)

# Returns RetrievedContext:
#   - contexts: List[Dict] - Relevant snippets with scores
#   - active_tasks: List[TaskState] - Current tasks
#   - sources: List[str] - Day IDs where context found
```

### MetadataExtractor
```python
extractor = MetadataExtractor(fallback_to_simple=True)
reply, metadata = extractor.parse_response(llm_output)

# Returns tuple:
#   - reply: str - Clean user-facing text
#   - metadata: dict with:
#       - keywords: List[str]
#       - summary: str
#       - affect: str
#       - parsing_method: 'structured' or 'fallback'
```

---

## 🎯 LLM System Prompt Format

```
==USER_REPLY_START==
Your natural language response here
==USER_REPLY_END==

==METADATA_START==
KEYWORDS: keyword1, keyword2, keyword3
SUMMARY: One-line summary of this turn
AFFECT: neutral|positive|negative|curious|frustrated|excited|confused|satisfied
==METADATA_END==
```

**Import the prompt:**
```python
from memory.metadata_extractor import MEMORY_SYSTEM_PROMPT
```

---

## 🧪 Testing Commands

```powershell
# Test crawler (5 comprehensive tests)
python test_crawler.py

# Test metadata extraction
python memory/metadata_extractor.py

# Test intent analyzer
python memory/retrieval/intent_analyzer.py

# Inspect database
python inspect_memory.py

# Run main with memory
python main.py
```

---

## 📁 File Structure

```
memory/
├── retrieval/
│   ├── __init__.py
│   ├── crawler.py           ✅ Search engine (324 lines)
│   └── intent_analyzer.py   ✅ Keyword extraction (243 lines)
├── metadata_extractor.py    ✅ LLM parser (421 lines)
├── storage.py               ✅ Database (900 lines)
├── conversation_manager.py  ✅ Turn logging (260 lines)
├── models.py                ✅ Data structures (475 lines)
└── __init__.py

tests/
└── test_crawler.py          ✅ Test suite (308 lines)

docs/
├── INTEGRATION_GUIDE.md     📄 Step-by-step integration
├── PHASE_2_COMPLETE.md      📄 Full component reference
├── RETRIEVAL_SYSTEM_PLAN.md 📄 Design rationale
└── FLOWCHART_ALIGNMENT_ANALYSIS.md 📄 Progress tracking
```

---

## ⚙️ Configuration Options

```python
# Crawler settings
crawler = LatticeCrawler(
    storage=storage,
    max_days_back=7  # How many days to search backward
)

# Retrieval settings
context = crawler.retrieve_context(
    intent=intent,
    current_day_id=today,
    max_results=5  # Top N results to return
)

# Metadata extraction
extractor = MetadataExtractor(
    fallback_to_simple=True  # Auto-fallback if parsing fails
)

# Intent analyzer
analyzer = IntentAnalyzer(
    use_llm_mode=False  # True to parse LLM-provided metadata
)
```

---

## ✅ Success Indicators

After integration, you should see:

1. **Console Output:**
   ```
   🔍 Intent: memory_query, Keywords: ['discuss', 'memory', 'systems']
   📅 Search range: 7 days (2025-10-10 to 2025-10-04)
   📝 Found 3 keyword matches
   ✅ Retrieved context: 3 snippets, 2 sources
   ✅ Metadata extracted: 5 keywords
   ```

2. **Database (inspect_memory.py):**
   - Keywords saved for each turn
   - Multiple sessions linked to same day
   - Full conversation text persisted

3. **User Experience:**
   - Clean responses (no visible metadata)
   - Context from past sessions retrieved
   - Relevant information recalled

---

## ⚠️ Troubleshooting

| Problem | Solution |
|---------|----------|
| **No contexts retrieved** | • Check keywords are being saved (run `inspect_memory.py`)<br>• Verify crawler is initialized<br>• Check day_id format is YYYY-MM-DD |
| **Always fallback mode** | • LLM not outputting delimiters<br>• Check MEMORY_SYSTEM_PROMPT is in prompt<br>• Verify LLM can follow structured format |
| **Import errors** | • Check `memory/__init__.py` exports<br>• Verify file paths are correct<br>• Try standalone tests first |
| **Database errors** | • Check database path exists<br>• Verify Storage initialized<br>• Check file permissions |
| **Low relevance scores** | • Add more test data<br>• Tune scoring weights in crawler<br>• Use more specific keywords |

---

## 💡 Tips & Best Practices

### **1. Progressive Enhancement**
Start simple, enhance later:
```python
# Week 1: Basic retrieval
context = crawler.retrieve_context(intent, today)

# Week 2: Add sliding window check
if not sliding_window.has_topic(intent.keywords):
    context = crawler.retrieve_context(intent, today)

# Week 3: Add context hydration
context = hydrator.build_prompt(sliding_window, context)
```

### **2. Monitor Metadata Quality**
```python
# Track structured vs fallback ratio
if metadata['parsing_method'] == 'structured':
    structured_count += 1
else:
    fallback_count += 1

# Log ratio for tuning
print(f"Structured: {structured_count}, Fallback: {fallback_count}")
```

### **3. Context Formatting**
```python
# Simple version
context_str = "\n".join([c['context'] for c in contexts])

# Enhanced version
context_str = ""
for ctx in contexts[:5]:
    context_str += f"\n[{ctx['day_id']}] {ctx['keyword']}: {ctx['context'][:100]}"
```

### **4. Error Handling**
```python
try:
    context = crawler.retrieve_context(intent, today)
except Exception as e:
    print(f"⚠️ Retrieval failed: {e}")
    context = RetrievedContext()  # Empty context
```

---

## 🎯 Next Steps

### **This Week:**
1. [ ] Add imports to main.py
2. [ ] Initialize components
3. [ ] Modify conversation loop
4. [ ] Test with real queries
5. [ ] Monitor metadata quality

### **Next Week (Phase 3):**
6. [ ] Build SlidingWindow class
7. [ ] Add "already loaded" check
8. [ ] Implement topic tracking
9. [ ] Build context_hydrator.py

### **Week 3 (Phase 4):**
10. [ ] Day synthesis
11. [ ] Task integration
12. [ ] End-of-day processing

---

## 📚 Documentation

- **INTEGRATION_GUIDE.md** - Complete integration walkthrough
- **PHASE_2_COMPLETE.md** - Full Phase 2 summary
- **RETRIEVAL_SYSTEM_PLAN.md** - Design decisions & rationale  
- **FLOWCHART_ALIGNMENT_ANALYSIS.md** - Progress tracking

---

## 🎉 Current Status

**✅ Phase 1 COMPLETE:** Storage, Models, Turn Logging  
**✅ Phase 2 COMPLETE:** Retrieval, Intent Analysis, Metadata Extraction  
**⚠️ Phase 3 NEXT:** Sliding Window, Context Hydration, Integration  
**🔵 Phase 4 FUTURE:** Day Synthesis, Task Migration

---

## Import Everything

```python
from memory import (
    # Types
    TaskStatus, TaskType, QueryType, ContextSourceType,
    
    # Core Models  
    DayNode, Keyword, Summary, Affect, TaskState,
    
    # Retrieval
    Intent, RetrievedContext,
    
    # Conversation
    ConversationTurn, SlidingWindow,
    
    # Storage & Management
    Storage, ConversationManager,
    
    # Utils
    create_day_id, create_task_id
)

# Retrieval Components
from memory.retrieval.crawler import LatticeCrawler
from memory.retrieval.intent_analyzer import IntentAnalyzer

# Metadata Extraction
from memory.metadata_extractor import MetadataExtractor, MEMORY_SYSTEM_PROMPT
```
)
```

## Common Patterns

### Create a Day
```python
from datetime import datetime

day = DayNode(
    day_id=create_day_id(),
    created_at=datetime.now(),
    session_ids=["session_20251010_120000"]
)
```

### Create a Task
```python
task = TaskState(
    task_id=create_task_id(TaskType.RECURRING_PLAN),
    task_type=TaskType.RECURRING_PLAN,
    status=TaskStatus.ACTIVE,
    created_date=create_day_id(),
    created_at=datetime.now(),
    last_updated=datetime.now(),
    task_title="30-day rowing challenge",
    total_steps=30,
    completed_steps=7
)

# Check progress
if task.progress_percentage() >= 50:
    print("Halfway there!")
```

### Track Keywords
```python
keyword = Keyword(
    keyword="rowing",
    first_mentioned=datetime.now(),
    last_mentioned=datetime.now(),
    turn_ids=[1]
)

# Later, when mentioned again
keyword.increment(turn_id=5)
```

### Analyze Intent
```python
intent = Intent(
    keywords=["rowing", "progress", "how"],
    query_type=QueryType.TASK_UPDATE,
    confidence=0.85,
    primary_topics=["rowing"]
)
```

### Manage Sliding Window
```python
window = SlidingWindow(max_turns=20)

if not window.is_topic_active("rowing"):
    # Need to retrieve rowing context
    window.mark_topic_active("rowing")
else:
    # Already have rowing context
    pass
```

### Build Retrieved Context
```python
context = RetrievedContext()

# Add from day keywords
context.add_context(
    {"date": "2025-10-03", "content": "Started rowing"},
    source="day_keyword"
)

# Add active tasks
context.active_tasks.append(task)

# Check size
print(f"Total snippets: {len(context.contexts)}")
print(f"Sources: {context.sources}")
```

## Enum Values

### TaskStatus
- `TaskStatus.ACTIVE`
- `TaskStatus.PAUSED`
- `TaskStatus.COMPLETED`
- `TaskStatus.CANCELLED`

### TaskType
- `TaskType.DISCRETE`
- `TaskType.RECURRING_PLAN`
- `TaskType.ONGOING_COMMITMENT`

### QueryType
- `QueryType.CHAT`
- `QueryType.TASK_REQUEST`
- `QueryType.TASK_UPDATE`
- `QueryType.MEMORY_QUERY`

## Useful Methods

### TaskState
- `task.progress_percentage()` → float (0-100)
- `task.to_dict()` → dict

### Keyword
- `keyword.increment(turn_id)` → Update frequency

### Affect
- `affect.update(turn_id, intensity, topics)` → Update pattern

### RetrievedContext
- `context.add_context(data, source)` → Add snippet

### SlidingWindow
- `window.is_topic_active(topic)` → bool
- `window.mark_topic_active(topic)` → None
- `window.add_turn(turn)` → None (auto-prunes if needed)

## Integration with Existing Code

```python
# Your existing code
from core.cognitive_lattice import CognitiveLattice

lattice = CognitiveLattice()
print(f"Session: {lattice.session_id}")

# New memory models
from memory import DayNode, create_day_id

day = DayNode(
    day_id=create_day_id(),
    created_at=datetime.now(),
    session_ids=[lattice.session_id]
)

# They work together!
print(f"Today: {day.day_id}")
print(f"Session: {lattice.session_id}")
```

## Testing

```bash
# Test models directly
python memory/models.py

# Test package imports
python -c "from memory import DayNode; print('Works!')"

# Test with existing system
python -c "from core.cognitive_lattice import CognitiveLattice; from memory import TaskState; print('Compatible!')"
```

## Next Steps

After `storage.py` is created:
```python
from memory.storage import Storage

storage = Storage("memory.db")
storage.save_day(day)
storage.save_task(task)

loaded_day = storage.get_day("2025-10-10")
```
