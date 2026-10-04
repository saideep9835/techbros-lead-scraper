---
name: techbros-lead-scraper
description: Use this skill whenever the user (Tech Bros AI Studio) asks to find new local business leads, run/check the daily Google Maps lead scraper, review today's new leads, draft outreach for scraped leads, or manage the lead-generation pipeline for Princeton TX and nearby cities. Trigger on phrases like "find me leads", "run the scraper", "check today's leads", "any new leads today", "draft outreach for these leads", or "update my lead list".
---

# Tech Bros AI Studio — Lead Generation Skill

## What this skill covers

Tech Bros AI Studio builds websites and AI tools (chatbots, voice agents,
automations) for small businesses. Its main lead source is local businesses on
Google Maps that have no website, an outdated website, or a low online
presence — scraped daily via an automated Apify pipeline (see the parent
`techbros-lead-scraper` project: `scraper.py`, `config.json`, GitHub Actions
workflow).

## When the user asks to find/check leads

1. If they have the project set up with GitHub Actions, point them to their
   repo's `data/leads_master.csv` (or the day's `new_leads_YYYY-MM-DD.csv`)
   rather than re-scraping — the pipeline already runs daily on its own.
2. If they want a fresh one-off search (e.g. a new city or niche not in their
   config), use the Apify tools directly:
   - Actor: `ittechinnovators/google-maps-business-scraper-lead-generator`
   - Inputs: `searchingKeywords` (business type), `location` (city, state),
     `maxResults`, `minRating`, `leadFilter` (`ALL` / `HOT_ONLY` / `HOT_WARM`)
   - Always confirm niche, city list, and an approximate lead count/cost with
     the user before running — this is a paid action (~$0.01/result).

## When the user asks to draft outreach for leads

Default style: short, specific, no generic templates. For each lead, reference:
- The business name specifically
- One concrete gap (no website / outdated site / hard to find online)
- A soft, low-pressure call to action (free 30-min consult, free mockup)

Example structure:
```
Subject: Quick question about [Business Name]'s website

Hi [Name/team],

I came across [Business Name] on Google Maps and noticed [specific gap —
e.g. "there's no website listed, so customers searching for you online
might be landing on nothing"]. 

We build affordable, mobile-friendly websites for local businesses like
yours (starting at $299), plus optional AI tools like chatbots that answer
customer questions 24/7.

Would you be open to a free 15-minute call to see if it's a fit?

[Name]
Tech Bros AI Studio
techbros.websitestudio@gmail.com
```

Never fabricate a business's details — only use what's in the scraped data
or what the user provides.

## When the user asks about the pipeline itself

Refer to the parent project's README.md for:
- How the GitHub Actions daily schedule works
- How to change cities/keywords (`config.json`)
- How the automated outreach works (`outreach.py`, `outreach_config.json`,
  `retell_agent_prompt.txt`) — calls leads with a phone via Retell AI,
  emails leads without one, logs uncontactable leads for manual follow-up
- How to extend it further (Google Sheets, Zapier/Make triggers)

## Boundaries

- Never send outreach messages or trigger calls on the user's behalf
  directly from this chat without explicit confirmation of the exact
  message/recipient — the automated pipeline (outreach.py) is something the
  user runs themselves, not something to trigger from a conversation.
- Always state estimated Apify cost before running a new scrape.
- Don't invent lead data — only report what the scraper/tool actually returned.
- If asked to change the outreach message, qualifying question, or call
  script, edit `retell_agent_prompt.txt` and/or `outreach_config.json`
  rather than describing changes only in chat.
