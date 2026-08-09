# Translationese Review

## Non-negotiable release rule

Do not ask only whether a native reader can understand the target. Ask whether a native writer, working from the same intent without seeing the source, would naturally choose substantially the same wording.

Fail the target when the answer is clearly no. Correct grammar cannot compensate for non-native phrasing, just as fluent phrasing cannot compensate for changed meaning.

## Review agent-written target text

1. Hide the source and read the target as original writing.
2. Mark wording that is grammatical but unlikely in the stated locale, register, domain, or medium.
3. Check collocations, parallel structure, paragraph flow, terminology, information order, and precision—not only spelling and grammar.
4. Distinguish established target-language terminology from unnecessary source-language borrowing. Do not ban loanwords mechanically.
5. Replace vague AI filler with the concrete relationship or operation already supported by the source.
6. Rewrite from intent at clause or paragraph level. Do not preserve the candidate's sentence skeleton by swapping synonyms.
7. Compare the rewrite with the source and restore any omitted limitation, uncertainty, fact, or relationship.

## Common agent failure patterns

| Pattern | Why it fails | Required response |
| --- | --- | --- |
| Every sentence is grammatical, but the paragraph feels translated | Grammar was checked without a target-only native edit | Rewrite the paragraph from its meaning map |
| A list mixes products, activities, domains, and abstractions | Source categories were copied instead of expressed natively | Rebuild the list with parallel target-language forms |
| A source-language technical word appears although native terminology is normal | The model preserved a familiar token instead of choosing for the audience | Use the established target term unless the loanword is conventional |
| “Smart,” “seamless,” “appropriate,” or similar filler replaces the actual operation | The copy sounds polished while losing precision | Name what the system actually does without inventing a claim |
| Casual and formal words alternate inside one product paragraph | Sentences were optimized independently | Apply one audience, register, and product voice to the whole passage |

## Swedish regression case: BLUN product copy

Target specification: contemporary neutral Swedish, professional European technology product copy, direct address, suitable for a public website.

### Agent-written candidate

> BLUN samlar kraftfulla AI-modeller med kompletta arbetsytor för appar, webbplatser, mjukvaruutveckling, språk, bilder och automatiserade flöden. Istället för att byta verktyg för varje steg jobbar modeller, agenter, API:er och MCP-servrar tillsammans på en gemensam europeisk plattform.
>
> Med BLUN planerar du komplexa projekt, skriver professionell kod, analyserar dokument, bygger webbappar, skapar desktop- och mobilprogramvara, transkriberar samtal och genererar eller redigerar bilder. Du kan välja modell själv eller låta BLUN fördela uppgiften på ett smart sätt.

The candidate is understandable and largely grammatical, but it does not pass the native release gate:

- `appar, webbplatser, mjukvaruutveckling, språk, bilder och automatiserade flöden` mixes outputs, an activity, domains, media, and a process in one noun list;
- `jobbar` is more conversational than the surrounding professional product voice;
- `desktop- och mobilprogramvara` carries an avoidable English form where native phrasing is clearer;
- `fördela uppgiften på ett smart sätt` is generic AI filler and does not say what is distributed or how;
- `Istället` is understandable, while `I stället` is the more conservative choice for polished standard copy; treat this as a style decision, not the main defect.

### Native rewrite

> BLUN samlar kraftfulla AI-modeller och kompletta arbetsytor för att bygga appar och webbplatser, utveckla programvara, arbeta med språk och bilder samt automatisera arbetsflöden. I stället för att byta verktyg i varje steg arbetar modeller, agenter, API:er och MCP-servrar tillsammans på en gemensam europeisk plattform.
>
> Med BLUN kan du planera komplexa projekt, skriva professionell kod, analysera dokument, bygga webbappar, utveckla programvara för datorer och mobila enheter, transkribera samtal samt generera eller redigera bilder. Du väljer själv vilken modell du vill använda – eller låter BLUN automatiskt fördela arbetet mellan de modeller som passar bäst för uppgiften.

The rewrite keeps the supported capabilities and European-platform claim. It rebuilds the mixed list as parallel actions, aligns the register, removes the avoidable Anglicism, and replaces vague “smart” wording with the intended model-selection relationship.

### Second agent-written candidate

> BLUN samlar kraftfulla AI-modeller och fullständiga arbetsytor för appar, webbplatser, mjukvaruutveckling, språk, bilder och automatiserade flöden. I stället för att öppna ett nytt verktyg för varje steg samverkar modeller, agenter, API:er och MCP-servrar på en gemensam europeisk plattform.
>
> Med BLUN planerar du komplexa projekt, skriver professionell kod, analyserar dokument, bygger webbappar, utvecklar programvara för dator och mobil, transkriberar samtal och skapar eller redigerar bilder. Du väljer modell själv – eller låter BLUN fördela uppgiften till den modell som passar bäst.

This revision is smoother but still fails:

- the opening list still mixes products, activities, media, and processes instead of using parallel actions;
- `fullständiga arbetsytor` is possible but sounds more source-shaped here than the established product phrase `kompletta arbetsytor`;
- `programvara för dator och mobil` is understandable but less polished and precise than `programvara för datorer och mobila enheter`;
- `fördela uppgiften till den modell` uses `fördela` as if it meant selecting one recipient. Swedish naturally uses `välja` for this relationship;
- the source claim that model selection happens automatically is no longer explicit.

For a system that chooses one suitable model, use:

> Du väljer själv vilken modell du vill använda – eller låter BLUN automatiskt välja den modell som passar bäst för uppgiften.

This case prevents a common repair failure: improving the rhythm while leaving the underlying verb relationship or source claim incorrect.

## Generalize the lesson

Apply the same test in every language and script. Do not memorize the Swedish replacements as a blacklist. Detect the underlying defect: source-shaped wording survived because the agent judged correctness from grammar and word correspondence instead of native usage, paragraph logic, register, and precision.
