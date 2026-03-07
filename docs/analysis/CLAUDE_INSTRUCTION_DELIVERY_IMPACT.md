# Claude Instruction Delivery Methods - Behavioral Analysis

**Question**: Yêu cầu Claude đọc instructions từ endpoint vs thông qua skill file - có ảnh hưởng gì?

**Answer**: Có ảnh hưởng **rất lớn** đến:
1. Context window usage
2. Instruction prioritization
3. Long-term memory
4. Reliability
5. Cost

---

## Approach 1: Yêu Cầu Claude Đọc Endpoint

```
User to Claude:
"Please fetch instructions from https://deb-0.vietml.com/setup 
and follow them to set up orchestration."
```

### How It Works

**Step 1**: Claude reads request in conversation
```
User's message context: ~500 tokens
```

**Step 2**: Claude decides to fetch endpoint
- Makes HTTP call to /setup
- Gets response (e.g., 2000 tokens)
- Total in context now: ~2500 tokens

**Step 3**: Claude processes instructions
- Parses JSON/markdown response
- Integrates into context window
- Uses it for subsequent calls

**Step 4**: Claude forgets after conversation
- Next session: No memory of instructions
- Must refetch every time
- Or user must re-paste

### Pros

✅ **Dynamic**: Node can update instructions without code changes
✅ **Centralized**: Single source of truth
✅ **Flexible**: Can serve different instructions based on query params
✅ **Current**: Always latest version

### Cons

❌ **Context Overhead**: Fetching adds tokens to every conversation start
❌ **Latency**: HTTP request delay (~100-500ms)
❌ **Forgetting**: Lost after conversation ends
❌ **Fragile**: If endpoint down, instructions unavailable
❌ **Cost**: Extra tokens per session
❌ **Reliability**: Network dependent

### Token Cost Analysis

**Single conversation**:
- User prompt: 50 tokens
- Claude decides to fetch: 100 tokens
- HTTP response (instructions): 2000 tokens
- Claude processing: 500 tokens
- **Total for just setup**: ~2650 tokens

**Yearly cost** (assuming 100 conversations/month):
- 2650 tokens × 100 × 12 = 3.18M tokens/year
- At $3/1M input tokens = **~$9.54/year just for fetching instructions**

---

## Approach 2: Embed Instructions in Skill File

```
# /mnt/skills/user/gnot-orchestration/SKILL.md

Contains full instructions for GNOT orchestration
Claude automatically reads when task triggers
```

### How It Works

**Setup (One-time)**:
- Create skill file with instructions
- Claude system reads it at startup
- Integrated into Claude's system prompt

**During Conversation**:
- User mentions "GNOT" or orchestration
- Skill file **already loaded** in context
- Claude immediately knows how to help
- No fetch needed

**After Conversation**:
- Skill knowledge persists across sessions
- Next user asking about GNOT: Claude already knows
- No network calls needed

### Pros

✅ **Zero latency**: Instructions already available
✅ **No network overhead**: No HTTP calls
✅ **Persistent memory**: Available in every session
✅ **Cost efficient**: No token overhead for fetching
✅ **Reliable**: Works even if node offline
✅ **Automatic**: Claude detects trigger keywords
✅ **Composable**: Can link multiple skills

### Cons

❌ **Static**: Must update manually if instructions change
❌ **Manual distribution**: Need to update skill file in repo
❌ **Version control**: Need to manage versions
❌ **Latency**: Requires Claude app update (if using skills)

### Token Cost Analysis

**One-time setup**:
- Create skill file: 0 tokens (one-time effort)
- Stored as part of skill system: ~500-1000 tokens (amortized across all users)

**Per conversation**:
- No fetch needed
- Instructions available from system context
- **Zero additional tokens for instructions**

**Yearly cost**:
- 0 (amortized system cost shared across all users)

---

## Comparison Table

| Factor | Fetch Endpoint | Skill File |
|--------|---|---|
| **Initial Setup** | 2 minutes | 10 minutes |
| **Token Cost Per Chat** | ~2000-3000 | ~0 |
| **Network Calls** | Yes (every chat) | No |
| **Latency** | ~200-500ms | 0ms |
| **Instructions Update** | Instant | Manual |
| **Persistence** | No (per-chat only) | Yes (cross-session) |
| **Works Offline** | ❌ No | ✅ Yes |
| **Reliability** | Depends on endpoint | 100% |
| **Auto-discovery** | ✅ Yes (if user knows URL) | ✅ Yes (if skill triggered) |
| **Cost Efficiency** | ❌ Expensive | ✅ Free |

---

## Claude's Actual Behavior Differences

### Scenario 1: With Endpoint Fetch

**User**: "I have a GNOT node at deb-0.vietml.com"

**Claude's thought process**:
```
1. Read user message (50 tokens)
2. Recognize "GNOT node" + URL
3. Decide: "I should fetch instructions"
4. Make HTTP request to /setup (or similar)
5. Receive 2000-token response
6. Parse and integrate into context
7. Now have 2500+ tokens in window
8. "I can help! Let me analyze..."
9. Available context for orchestration tasks: 95% of window
10. After chat: Instructions forgotten
```

**Next session**: Entire process repeats

### Scenario 2: With Skill File

**User**: "I want to orchestrate my GNOT mesh"

**Claude's thought process**:
```
1. Read user message (50 tokens)
2. Recognize "orchestrate" + "GNOT"
3. Skill system triggers GNOT skill
4. Skill already loaded: "I know GNOT!"
5. Instructions available in context (not using extra tokens)
6. Available context for orchestration: 99%+ of window
7. "I can help! Here's how..."
8. After chat: Instructions still known
```

**Next session**: User says "Orchestrate disk check on deb-0"
```
Claude immediately knows:
- deb-0 is the node
- mesh_action is the tool
- How to structure requests
- No need to refetch or re-explain
```

---

## Real-World Impact Examples

### Example 1: Simple Task

**Task**: "Check disk usage on deb-0"

**With Endpoint Fetch**:
```
User: "Check disk usage on deb-0"
Claude: "Let me fetch the setup instructions..."
        [Makes HTTP request - 200ms delay]
        [Reads 2000 tokens of instructions]
        "Now I understand. Let me call mesh_action..."
Total time: ~500ms + processing
Context used: 2650 tokens
```

**With Skill File**:
```
User: "Check disk usage on deb-0"
Claude: "I'll check disk usage on deb-0..."
        [Already knows from skill]
        "Calling mesh_action..."
Total time: Immediate (no fetch)
Context used: ~100 tokens
```

**Difference**: 400-500ms faster, uses 96% fewer tokens

---

### Example 2: Complex Multi-Node Orchestration

**Task**: "Check disk on deb-0, memory on deb-1, compare, recommend cleanup"

**With Endpoint Fetch**:
```
User: Long question
Claude: "Let me fetch setup instructions first..."
        [HTTP request, read 2000 tokens]
        "OK now I have context. Let me start..."
        - Call deb-0 for disk (need to reference instructions)
        - Poll result
        - Call deb-1 for memory (need to reference instructions again)
        - Poll result
        - Compare
        
Total context used: ~3500 tokens (out of 100k)
Total time: 500ms + 4 API calls
Cost: 3500 tokens
```

**With Skill File**:
```
User: Long question
Claude: "I'll orchestrate that across both nodes..."
        [Already knows from skill - no fetch]
        [Can reuse learned patterns]
        - Call deb-0 for disk
        - Poll result
        - Call deb-1 for memory
        - Poll result
        - Compare
        
Total context used: ~2000 tokens (no instruction fetch)
Total time: Immediate start + 4 API calls
Cost: 2000 tokens
```

**Difference**: Save 1500 tokens, start 500ms faster

---

## Advanced: Claude Behavior with Skills

### What Skills Actually Do

When Claude.ai uses skills:

```
┌─────────────────────────────────────────┐
│ Claude System Prompt (Always loaded)    │
├─────────────────────────────────────────┤
│ Core instructions (5000 tokens)         │
│ Behavior guidelines                     │
│ Tool definitions                        │
├─────────────────────────────────────────┤
│ Skill Files (When triggered)            │
├─────────────────────────────────────────┤
│ gnot-orchestration.md (loaded when     │
│   user mentions GNOT)                   │
│                                         │
│ Content:                                │
│ - How GNOT works                        │
│ - mesh_action tool definition           │
│ - Example patterns                      │
│ - API details                           │
│                                         │
│ Always available in this session        │
│ Lost after session ends                 │
│ (But user can reference in next session)│
└─────────────────────────────────────────┘
```

### Smart Behavior: Claude with Skills

When Claude has skill loaded:

```python
# In its reasoning:
if user_asks_about("GNOT", "mesh", "orchestration"):
    # Skill already loaded
    use_skill_knowledge()
    
    # Behaves more intelligently:
    - Doesn't ask "how do I call this?"
    - Remembers previous patterns from session
    - Can chain actions automatically
    - Suggests optimizations
    
    # Example:
    "Since disk is 84% full on deb-0, I should 
     also check available space on deb-1 to 
     recommend consolidation..."
    
    # Without skill:
    "I'll check disk on deb-0 as requested"
```

---

## Hybrid Approach (Best)

Combine both for optimal behavior:

```
Step 1: Skill File (Long-term)
┌──────────────────────────────────────┐
│ skills/gnot-orchestration/SKILL.md   │
│                                      │
│ • How GNOT works (overview)          │
│ • Common orchestration patterns      │
│ • Tool definitions                   │
│ • Best practices                     │
│                                      │
│ Loaded: Once per session             │
│ Persistent: Within session           │
│ Cost: 0 tokens (amortized)          │
└──────────────────────────────────────┘
         ↓
Step 2: Endpoint (Short-term)
┌──────────────────────────────────────┐
│ GET /bootstrap (on demand)           │
│                                      │
│ • Current node status               │
│ • Available actions                 │
│ • Live examples                     │
│ • Node-specific config              │
│                                      │
│ Loaded: Only if user asks           │
│ Fetch: Optional, when needed        │
│ Cost: ~2000 tokens only if needed   │
└──────────────────────────────────────┘
```

**User experience**:
```
Session 1:
User: "I want to use my GNOT nodes"
Claude: [Skill loaded] "I know GNOT! What would you like?"
User: "Get setup details"
Claude: [Fetches /bootstrap endpoint] "Here's current status..."

Session 2 (new conversation):
User: "Orchestrate my mesh"
Claude: [Skill loaded again] "I remember GNOT from before. Let me help..."
(No need to fetch - skill has the general knowledge)
```

---

## Token Economics: Which to Choose?

### Scenario A: Heavy GNOT Usage (20+ chats/month)

**Option 1: Fetch Endpoint**
- 20 chats × 2000 tokens = 40,000 tokens/month
- Cost: $0.12/month
- **Total/year: $1.44**

**Option 2: Skill File**
- 0 tokens for instructions
- Cost: $0/month
- **Total/year: $0**

**Winner**: Skill File (100% savings)

---

### Scenario B: Casual GNOT Usage (2 chats/month)

**Option 1: Fetch Endpoint**
- 2 chats × 2000 tokens = 4,000 tokens/month
- Cost: $0.012/month
- **Total/year: $0.14**

**Option 2: Skill File**
- Setup effort: 10 minutes
- Cost: Free
- **Total/year: $0**

**Winner**: Still skill file (practically free)

---

### Scenario C: Dynamic Node Configuration (new node every week)

**Option 1: Fetch Endpoint**
- Automatically returns latest config
- No code changes needed
- Always up-to-date

**Option 2: Skill File**
- Must manually update skill
- Lag between node creation and documentation
- More overhead

**Winner**: Endpoint fetch (for dynamic scenarios)

---

## Practical Recommendation

### Use Skill File For:
- ✅ Static, long-term knowledge
- ✅ Best practices and patterns
- ✅ Tool definitions (not changing)
- ✅ Common use cases
- ✅ Budget-conscious teams

### Use Endpoint For:
- ✅ Dynamic information (live node status)
- ✅ Current configuration
- ✅ Real-time examples
- ✅ Large deployments with frequent changes
- ✅ Auto-generated documentation

### Hybrid (Recommended):
```
Skill File: General GNOT knowledge, patterns, best practices
  + 
Endpoint: Live node status, specific configurations, current actions

Result: Claude is smart AND has current info, with low cost
```

---

## Code Example: What Claude Sees

### With Endpoint Fetch

```
User Message (in conversation):
{
  "role": "user",
  "content": "I have a GNOT node. Please setup and check disk."
}

Claude's Context Window:
- User message: ~100 tokens
- /setup response fetched: ~2000 tokens
- Parsing + thinking: ~500 tokens
- Available for response: ~97400 tokens

Claude's tool calls:
- Fetch /setup
- Parse response
- Call mesh_action
- Poll result
```

### With Skill File

```
System Prompt includes:
{
  "role": "system",
  "content": "...system instructions... 
  
  SKILL: gnot-orchestration
  You have knowledge of GNOT mesh orchestration...
  [full skill content ~1500 tokens]"
}

User Message (in conversation):
{
  "role": "user",
  "content": "I have a GNOT node. Please setup and check disk."
}

Claude's Context Window:
- System (with skill): ~6500 tokens
- User message: ~100 tokens
- Claude thinking: ~500 tokens
- Available for response: ~93000 tokens

Claude's tool calls:
- No fetch (skill already in context)
- Call mesh_action directly
- Poll result
```

**Difference**: ~2000 tokens saved per conversation

---

## Summary: Claude Behavior Impact

| Aspect | Endpoint Fetch | Skill File |
|--------|---|---|
| **Latency** | Slower (fetch) | Faster (loaded) |
| **Memory** | Forgets after chat | Remembers in session |
| **Cost** | ~$0.12/month (20 chats) | $0 |
| **Intelligence** | Good (has instructions) | Better (integrated knowledge) |
| **Reliability** | Depends on node | 100% |
| **Pattern Recognition** | Learns within chat | Can build on previous chats |
| **Hallucination Risk** | Lower (has reference) | Higher (if skill outdated) |
| **Proactive Help** | Unlikely | Likely (knows context) |
| **Context Efficiency** | Poor | Excellent |

---

## Conclusion

**YÊU CẦU CLAUDE FETCH ENDPOINT**:
- ✅ Always accurate (real-time from node)
- ❌ Expensive (2000 tokens per session)
- ❌ Slow (HTTP round-trip)
- ❌ Forgets between sessions
- ❌ Context bloated

**SKILL FILE APPROACH**:
- ✅ Fast (no network call)
- ✅ Cheap (0 tokens for instructions)
- ✅ Remembers within session
- ✅ Clean context
- ❌ Static (need manual updates)

**BEST**: Hybrid
- Skill file for general knowledge
- Endpoint fetch for live/specific data
- Claude stays smart AND current
- Minimal token overhead
- Maximum reliability
