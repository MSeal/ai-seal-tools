# Scheduling preferences

Free-form notes about your scheduling preferences. Claude reads this whole
file when ranking meeting slots and composing ask-messages. Be specific —
quotable sentences become the explanation when Claude applies them.

Delete entire sections, add new ones, write in your own voice. Nothing here
is required. The helper still scores conflicts and applies universal
heuristics (lunch, day-edges) without any of these guidelines.

## Defended time

Times you protect against meetings unless absolutely necessary. Be specific
about the conditions under which they CAN be overridden, so Claude knows
when to break the rule.

Examples:
- Tuesday and Thursday mornings, 9–11 PT, are deep-work blocks. Don't put
  meetings there unless every alternative this week has worse conflicts.
- Lunch (12–1 PT) is sacred — never propose, even for execs.

## Day-of-week preferences

How different days of the week rank for new meetings.

Examples:
- Mondays are recovery / focus days. Push meetings to Tue–Fri when possible.
- Friday afternoons are buffer — propose Friday mornings only.

## People

Per-person notes about how their conflicts behave. Use this to override the
generic movability classifier when you have first-hand context.

Examples:
- Sorabh and Jon are direct reports. Their 1:1s with me move easily.
- carol splits time between SF and NYC — anything with him needs TZ
  awareness, and his "DNS - OOO" blocks usually mean travel days, not OOO.

## Ask-message tone

Voice for the pre-formatted messages Claude composes asking attendees to
move a conflicting meeting.

Examples:
- For peers: casual, brief, offer to handle the rescheduling yourself.
- For senior folks: apologetic, offer two alternatives instead of asking
  them to suggest one.
- For external / customer attendees: formal, never assume they can move.

## Anything else

Catchall for preferences that don't fit above. Avoid back-to-back meetings,
preferred meeting density per day, etc.
