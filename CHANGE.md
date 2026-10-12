---
title: Change Log
description: Project change notes for llm-microsoft-foundry
author: Microsoft
ms.date: 2026-10-12
ms.topic: reference
---

## 2026-10-12

Added support for decision model deployments such as
`Microsoft-Decision-1`.

The plugin now:

* Configures deployments as either `chat` or `decision`
* Sends decision requests to the Microsoft provider's `systemone` endpoint
* Accepts complete JSON requests or state text with a `questions` option
* Returns typed decision answers as JSON
* Records decision token usage in LLM response metadata
* Caches Entra tokens separately for chat and decision scopes

Existing model configurations continue to use the chat API by default.

## 2026-08-10

Added compatibility handling for models that reject `max_tokens`.

When a request fails with a Foundry 400 error indicating that `max_tokens`
is unsupported and `max_completion_tokens` must be used, the plugin now
retries the request once with `max_completion_tokens`.

This behavior is applied to both streaming and non-streaming requests.

### Why this change

Some Foundry model endpoints return:

* `unsupported_parameter`
* `param: max_tokens`
* Guidance to use `max_completion_tokens`

Before this update, those requests failed even though a compatible parameter
was available.

### Result

Existing commands that pass `-o max_tokens` continue to work for those models
without requiring command changes.
