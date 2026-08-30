var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// index.ts
var ALLOWED_INTENTS = /* @__PURE__ */ new Set(["chat", "meal_add", "meal_correction"]);
var PLACEHOLDER_REPLIES = /* @__PURE__ */ new Set([
  "\u65E5\u672C\u8A9E\u306E\u8FD4\u4FE1",
  "\u30E6\u30FC\u30B6\u30FC\u3078\u306E\u8FD4\u4FE1\u30E1\u30C3\u30BB\u30FC\u30B8",
  "\u8FD4\u4FE1\u30E1\u30C3\u30BB\u30FC\u30B8"
]);
var JST_TIME_ZONE = "Asia/Tokyo";
function formatError(error) {
  if (error instanceof Error) return `${error.name}: ${error.message}`;
  return String(error);
}
__name(formatError, "formatError");
function logEvent(event, fields = {}) {
  console.log(JSON.stringify({ event, ...fields }));
}
__name(logEvent, "logEvent");
function getDailyChatLimit(env) {
  const parsed = Number.parseInt(env.DAILY_CHAT_LIMIT || "20", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 20;
}
__name(getDailyChatLimit, "getDailyChatLimit");
function getJstDateKey(date = /* @__PURE__ */ new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: JST_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
__name(getJstDateKey, "getJstDateKey");
function getNextJstMidnightTimestamp(now = Date.now()) {
  const jstNow = new Date(now + 9 * 60 * 60 * 1e3);
  const nextJstDateAsUtc = Date.UTC(
    jstNow.getUTCFullYear(),
    jstNow.getUTCMonth(),
    jstNow.getUTCDate() + 1,
    0,
    0,
    0
  );
  return nextJstDateAsUtc - 9 * 60 * 60 * 1e3;
}
__name(getNextJstMidnightTimestamp, "getNextJstMidnightTimestamp");
function truncateForLine(text, maxLength = 5e3) {
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 1))}\u2026`;
}
__name(truncateForLine, "truncateForLine");
function looksLikeMealCorrection(text) {
  return /(じゃなくて|ではなくて|ではなく|代わりに|違いまし|間違い|訂正|修正|本当は|正しくは|さっき|先ほど|前の食事)/.test(text);
}
__name(looksLikeMealCorrection, "looksLikeMealCorrection");
function isPlaceholderReply(text) {
  return PLACEHOLDER_REPLIES.has(text.trim());
}
__name(isPlaceholderReply, "isPlaceholderReply");
var DailyLimitCounter = class {
  static {
    __name(this, "DailyLimitCounter");
  }
  state;
  constructor(state, _env) {
    this.state = state;
  }
  async fetch(request) {
    const url = new URL(request.url);
    const parsedLimit = Number.parseInt(url.searchParams.get("limit") || "20", 10);
    const limit = Number.isFinite(parsedLimit) && parsedLimit > 0 ? parsedLimit : 20;
    let count = await this.state.storage.get("count") || 0;
    if (count >= limit) {
      return Response.json({ allowed: false, count });
    }
    count += 1;
    await this.state.storage.put("count", count);
    const existingAlarm = await this.state.storage.getAlarm();
    if (existingAlarm === null) {
      await this.state.storage.setAlarm(getNextJstMidnightTimestamp());
    }
    return Response.json({ allowed: true, count });
  }
  async alarm() {
    await this.state.storage.delete("count");
  }
};
var index_default = {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }
    const url = new URL(request.url);
    if (url.pathname === "/webhook") {
      return handleWebhook(request, env, ctx);
    }
    return new Response("Not Found", { status: 404 });
  }
};
async function handleWebhook(request, env, ctx) {
  const body = await request.text();
  const signature = request.headers.get("X-Line-Signature") || "";
  if (!await verifyLineSignature(body, signature, env.LINE_CHANNEL_SECRET)) {
    return new Response("Invalid Signature", { status: 403 });
  }
  const payload = JSON.parse(body);
  const events = payload.events || [];
  for (const event of events) {
    ctx.waitUntil(processEvent(event, env));
  }
  return new Response("OK", { status: 200 });
}
__name(handleWebhook, "handleWebhook");
async function processEvent(event, env) {
  const replyToken = event.replyToken;
  try {
    await processEventInner(event, env);
  } catch (e) {
    console.error("processEvent failed:", formatError(e), e instanceof Error ? e.stack : "");
    logEvent("line_event", { stage: "failed", error: formatError(e) });
    await notifyLine(
      replyToken,
      event?.source?.userId,
      "\u51E6\u7406\u4E2D\u306B\u554F\u984C\u304C\u8D77\u304D\u307E\u3057\u305F\u3002\u5C11\u3057\u6642\u9593\u3092\u304A\u3044\u3066\u3001\u3082\u3046\u4E00\u5EA6\u304A\u8A66\u3057\u304F\u3060\u3055\u3044\u3002",
      env
    );
  }
}
__name(processEvent, "processEvent");
async function processEventInner(event, env) {
  const eventId = event.webhookEventId;
  if (await isDuplicateEvent(eventId, env)) {
    logEvent("line_event", { stage: "duplicate_ignored" });
    return;
  }
  const type = event.type;
  const replyToken = event.replyToken;
  const userId = event.source.userId;
  logEvent("line_event", {
    stage: "received",
    type,
    message_type: event.message?.type || null,
    has_reply_token: Boolean(replyToken)
  });
  if (type === "follow") {
    await notifyLine(
      replyToken,
      userId,
      "\u53CB\u3060\u3061\u8FFD\u52A0\u3042\u308A\u304C\u3068\u3046\u3054\u3056\u3044\u307E\u3059\uFF01\n\u98DF\u4E8B\u7BA1\u7406Bot\u3067\u3059\u3002\u5199\u771F\u304B\u30C6\u30AD\u30B9\u30C8\u3067\u98DF\u4E8B\u5185\u5BB9\u3092\u6559\u3048\u3066\u304F\u3060\u3055\u3044\u306D\u3002",
      env
    );
    return;
  }
  if (type === "unfollow" || type === "join" || type === "leave") {
    return;
  }
  if (type === "message") {
    const messageType = event.message.type;
    if (messageType === "text") {
      await handleTextMessage(event, env);
    } else if (messageType === "image") {
      logEvent("image_flow", { stage: "received" });
      await showLoadingAnimation(userId, 30, env);
      const ok = await proxyToRender(env, {
        endpoint: "/internal/image",
        payload: {
          user_id: userId,
          reply_token: replyToken,
          message_id: event.message.id
        }
      });
      logEvent("image_flow", { stage: ok ? "render_accepted" : "render_failed" });
      if (!ok) {
        await notifyLine(
          replyToken,
          userId,
          "\u5199\u771F\u306E\u51E6\u7406\u306B\u5931\u6557\u3057\u307E\u3057\u305F\u3002\u6050\u308C\u5165\u308A\u307E\u3059\u304C\u3001\u3082\u3046\u4E00\u5EA6\u9001\u3063\u3066\u304F\u3060\u3055\u3044\u3002",
          env
        );
      }
    }
  }
}
__name(processEventInner, "processEventInner");
async function handleTextMessage(event, env) {
  const text = event.message.text.trim();
  const userId = event.source.userId;
  const replyToken = event.replyToken;
  logEvent("text_flow", { stage: "received", text_length: text.length });
  if (text === "\u30EA\u30BB\u30C3\u30C8") {
    const ok = await proxyToRender(env, {
      endpoint: "/internal/text",
      payload: {
        user_id: userId,
        reply_token: replyToken,
        text,
        intent: null
      }
    });
    if (!ok) {
      await notifyLine(
        replyToken,
        userId,
        "\u30EA\u30BB\u30C3\u30C8\u51E6\u7406\u306B\u5931\u6557\u3057\u307E\u3057\u305F\u3002\u6050\u308C\u5165\u308A\u307E\u3059\u304C\u3001\u3082\u3046\u4E00\u5EA6\u304A\u8A66\u3057\u304F\u3060\u3055\u3044\u3002",
        env
      );
    } else {
      logEvent("text_flow", { stage: "render_accepted", route: "reset" });
    }
    return;
  }
  const fixedReply = getFixedCommandReply(text);
  if (fixedReply) {
    await notifyLine(replyToken, userId, fixedReply, env);
    logEvent("text_flow", { stage: "replied", route: "fixed_command" });
    return;
  }
  await showLoadingAnimation(userId, 30, env);
  let intent = null;
  let replyText = "";
  let aiSuccess = false;
  if (looksLikeMealCorrection(text)) {
    intent = "meal_correction";
    aiSuccess = true;
    logEvent("workers_ai", { outcome: "skipped", reason: "correction_phrase", intent });
  } else {
    try {
      const aiResult = await classifyWithWorkersAI(text, env);
      if (aiResult && ALLOWED_INTENTS.has(aiResult.intent)) {
        intent = aiResult.intent;
        replyText = aiResult.reply || "";
        aiSuccess = true;
      }
    } catch (e) {
      console.error("Workers AI Error:", formatError(e), e instanceof Error ? e.stack : "");
      logEvent("workers_ai", { outcome: "error", error: formatError(e) });
    }
  }
  logEvent("text_flow", {
    stage: "intent_classified",
    ai_success: aiSuccess,
    intent: intent || "none",
    route: intent === "chat" && aiSuccess ? "chat" : "render"
  });
  if (intent === "chat" && aiSuccess) {
    const limitResult = await checkAndIncrementDailyLimit(userId, env);
    if (!limitResult.allowed) {
      await notifyLine(
        replyToken,
        userId,
        `\u96D1\u8AC7\u6A5F\u80FD\u306F1\u65E5\u306E\u5229\u7528\u56DE\u6570\uFF08${limitResult.limit}\u56DE\uFF09\u306B\u9054\u3057\u307E\u3057\u305F\u3002\u98DF\u4E8B\u306E\u8A18\u9332\u306A\u3089\u7121\u5236\u9650\u3067\u3054\u5229\u7528\u3044\u305F\u3060\u3051\u307E\u3059\u3002`,
        env
      );
      logEvent("daily_limit", { allowed: false, limit: limitResult.limit, count: limitResult.count });
      return;
    }
    const remaining = Math.max(limitResult.limit - limitResult.count, 0);
    const counterNote = `

\uFF08\u672C\u65E5\u306E\u96D1\u8AC7: \u6B8B\u308A${remaining}/${limitResult.limit}\u56DE\uFF09`;
    const maxReplyLength = Math.max(1, 5e3 - counterNote.length);
    await notifyLine(
      replyToken,
      userId,
      truncateForLine(replyText || "\u3053\u3093\u306B\u3061\u306F\uFF01", maxReplyLength) + counterNote,
      env
    );
    logEvent("daily_limit", { allowed: true, limit: limitResult.limit, count: limitResult.count });
  } else {
    const ok = await proxyToRender(env, {
      endpoint: "/internal/text",
      payload: {
        user_id: userId,
        reply_token: replyToken,
        text,
        intent: aiSuccess ? intent : null
        // 失敗時はRender側でIntentを再判定
      }
    });
    logEvent("text_flow", {
      stage: ok ? "render_accepted" : "render_failed",
      route: aiSuccess ? intent : "legacy_gemini"
    });
    if (!ok) {
      await notifyLine(
        replyToken,
        userId,
        "\u51E6\u7406\u306B\u5931\u6557\u3057\u307E\u3057\u305F\u3002\u6050\u308C\u5165\u308A\u307E\u3059\u304C\u3001\u3082\u3046\u4E00\u5EA6\u9001\u3063\u3066\u304F\u3060\u3055\u3044\u3002",
        env
      );
    }
  }
}
__name(handleTextMessage, "handleTextMessage");
async function verifyLineSignature(body, signature, secret) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const signatureBuffer = await crypto.subtle.sign("HMAC", key, encoder.encode(body));
  const calculatedSignature = btoa(String.fromCharCode(...new Uint8Array(signatureBuffer)));
  return calculatedSignature === signature;
}
__name(verifyLineSignature, "verifyLineSignature");
async function isDuplicateEvent(eventId, env) {
  if (!eventId) return false;
  const key = `dup:${eventId}`;
  try {
    const existing = await env.DAILY_LIMIT_KV.get(key);
    if (existing) return true;
    await env.DAILY_LIMIT_KV.put(key, "1", { expirationTtl: 60 });
    return false;
  } catch (e) {
    console.error(
      "isDuplicateEvent failed, treating as non-duplicate:",
      formatError(e),
      e instanceof Error ? e.stack : ""
    );
    return false;
  }
}
__name(isDuplicateEvent, "isDuplicateEvent");
async function classifyWithWorkersAI(text, env) {
  const model = env.WORKERS_AI_MODEL || "@cf/meta/llama-3.1-8b-instruct-fast";
  const url = `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/ai/run/${model}`;
  const startedAt = Date.now();
  const prompt = `
You are a diet assistant. Classify the user input into one of: "chat", "meal_add", "meal_correction".
- "chat": General conversation, greetings, questions not about specific meal logging.
- "meal_add": User wants to log a new meal.
- "meal_correction": User says the previous meal log is wrong and wants it replaced or corrected. Look for phrases such as "\u3058\u3083\u306A\u304F\u3066", "\u3067\u306F\u306A\u304F", "\u672C\u5F53\u306F", "\u8A02\u6B63", "\u4FEE\u6B63", "\u3055\u3063\u304D\u306E", or "\u5148\u307B\u3069\u306E".
If "chat", provide a friendly reply in Japanese in the "reply" field.
If "meal_add" or "meal_correction", set "reply" to an empty string.
  Output ONLY one valid JSON object with exactly these fields: "intent" and "reply".
  For "chat", "reply" must directly answer the exact user input in natural Japanese.
  Never output placeholder text such as "\u65E5\u672C\u8A9E\u306E\u8FD4\u4FE1" or "\u30E6\u30FC\u30B6\u30FC\u3078\u306E\u8FD4\u4FE1\u30E1\u30C3\u30BB\u30FC\u30B8".
  For "meal_add" or "meal_correction", "reply" must be an empty string.
  Do not output markdown, explanations, or any text outside the JSON object.
Input: "${text.replace(/"/g, '\\"')}"
`;
  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.CF_API_TOKEN}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        prompt
      }),
      signal: AbortSignal.timeout(8e3)
    });
  } catch (e) {
    if (e instanceof Error && (e.name === "TimeoutError" || e.name === "AbortError")) {
      throw new Error("Workers AI \u30BF\u30A4\u30E0\u30A2\u30A6\u30C8\uFF088\u79D2\u4EE5\u5185\u306B\u5FDC\u7B54\u306A\u3057\uFF09");
    }
    throw new Error(`Workers AI fetch\u5931\u6557: ${formatError(e)}`);
  }
  if (!response.ok) {
    const bodyText = await response.text().catch(() => "");
    throw new Error(`AI API Error: HTTP ${response.status} ${bodyText.slice(0, 300)}`);
  }
  let result;
  try {
    result = await response.json();
  } catch (e) {
    throw new Error(`Workers AI\u30EC\u30B9\u30DD\u30F3\u30B9\u306EJSON\u89E3\u6790\u306B\u5931\u6557: ${formatError(e)}`);
  }
  const rawText = result.result?.response;
  if (typeof rawText !== "string") {
    throw new Error("Invalid response from AI\uFF08result.response\u304C\u6587\u5B57\u5217\u3067\u306F\u3042\u308A\u307E\u305B\u3093\uFF09");
  }
  const cleaned = rawText.replace(/```json|```/g, "").trim();
  const jsonText = extractFirstJsonObject(cleaned);
  if (!jsonText) {
    throw new Error(`Invalid JSON from AI\uFF08\u672C\u6587\u304C\u898B\u3064\u304B\u3089\u306A\u3044\uFF09: ${rawText.slice(0, 200)}`);
  }
  try {
    const parsed = JSON.parse(jsonText);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("JSON\u30AA\u30D6\u30B8\u30A7\u30AF\u30C8\u3067\u306F\u3042\u308A\u307E\u305B\u3093");
    }
    if (!ALLOWED_INTENTS.has(parsed.intent)) {
      throw new Error(`\u8A31\u53EF\u3055\u308C\u3066\u3044\u306A\u3044intent: ${String(parsed.intent)}`);
    }
    const parsedReply = typeof parsed.reply === "string" ? parsed.reply.trim() : "";
    const placeholderReplaced = parsed.intent === "chat" && isPlaceholderReply(parsedReply);
    const classification = {
      intent: parsed.intent,
      reply: placeholderReplaced ? "\u3054\u9023\u7D61\u3042\u308A\u304C\u3068\u3046\u3054\u3056\u3044\u307E\u3059\u3002\u98DF\u4E8B\u306E\u8A18\u9332\u3084\u6804\u990A\u306B\u3064\u3044\u3066\u3001\u6C17\u306B\u306A\u308B\u3053\u3068\u3092\u9001\u3063\u3066\u304F\u3060\u3055\u3044\u306D\u3002" : parsedReply
    };
    logEvent("workers_ai", {
      outcome: "success",
      model,
      intent: classification.intent,
      placeholder_replaced: placeholderReplaced,
      elapsed_ms: Date.now() - startedAt
    });
    return classification;
  } catch (e) {
    throw new Error(`Invalid JSON from AI\uFF08parse\u5931\u6557: ${formatError(e)}\uFF09: ${jsonText.slice(0, 200)}`);
  }
}
__name(classifyWithWorkersAI, "classifyWithWorkersAI");
function extractFirstJsonObject(text) {
  const start = text.indexOf("{");
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (char === "\\") {
        escaped = true;
      } else if (char === '"') {
        inString = false;
      }
      continue;
    }
    if (char === '"') {
      inString = true;
    } else if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(start, index + 1);
    }
  }
  return null;
}
__name(extractFirstJsonObject, "extractFirstJsonObject");
async function checkAndIncrementDailyLimit(userId, env) {
  const limit = getDailyChatLimit(env);
  const today = getJstDateKey();
  const id = env.DAILY_LIMIT_DO.idFromName(`${userId}:${today}`);
  const stub = env.DAILY_LIMIT_DO.get(id);
  const response = await stub.fetch(`https://daily-limit/check?limit=${limit}`);
  if (!response.ok) {
    const bodyText = await response.text().catch(() => "");
    throw new Error(`Daily Limit DO Error: HTTP ${response.status} ${bodyText.slice(0, 200)}`);
  }
  const result = await response.json();
  return { allowed: result.allowed, count: result.count, limit };
}
__name(checkAndIncrementDailyLimit, "checkAndIncrementDailyLimit");
async function proxyToRender(env, data) {
  const url = `${env.RENDER_URL}${data.endpoint}`;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Secret": env.INTERNAL_SECRET
      },
      body: JSON.stringify(data.payload),
      signal: AbortSignal.timeout(8e3)
    });
    if (!response.ok) {
      const bodyText = await response.text().catch(() => "");
      console.error(`proxyToRender: Render returned ${response.status} for ${data.endpoint}: ${bodyText}`);
      return false;
    }
    return true;
  } catch (e) {
    console.error(`proxyToRender: request to ${data.endpoint} failed: ${formatError(e)}`);
    return false;
  }
}
__name(proxyToRender, "proxyToRender");
async function replyToLine(replyToken, message, env) {
  try {
    const response = await fetch("https://api.line.me/v2/bot/message/reply", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`
      },
      body: JSON.stringify({
        replyToken,
        messages: [{ type: "text", text: truncateForLine(message) }]
      }),
      signal: AbortSignal.timeout(5e3)
    });
    if (!response.ok) {
      const bodyText = await response.text().catch(() => "");
      console.error(`LINE reply failed: HTTP ${response.status} ${bodyText.slice(0, 300)}`);
      return false;
    }
    return true;
  } catch (e) {
    console.error(`LINE reply request failed: ${formatError(e)}`);
    return false;
  }
}
__name(replyToLine, "replyToLine");
async function pushToLine(userId, message, env) {
  try {
    const response = await fetch("https://api.line.me/v2/bot/message/push", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`
      },
      body: JSON.stringify({
        to: userId,
        messages: [{ type: "text", text: truncateForLine(message) }]
      }),
      signal: AbortSignal.timeout(5e3)
    });
    if (!response.ok) {
      const bodyText = await response.text().catch(() => "");
      console.error(`LINE push failed: HTTP ${response.status} ${bodyText.slice(0, 300)}`);
      return false;
    }
    return true;
  } catch (e) {
    console.error(`LINE push request failed: ${formatError(e)}`);
    return false;
  }
}
__name(pushToLine, "pushToLine");
async function notifyLine(replyToken, userId, message, env) {
  if (replyToken && await replyToLine(replyToken, message, env)) {
    logEvent("line_delivery", { channel: "reply", outcome: "success" });
    return true;
  }
  if (userId) {
    const pushed = await pushToLine(userId, message, env);
    logEvent("line_delivery", { channel: "push", outcome: pushed ? "success" : "failed" });
    return pushed;
  }
  console.error("LINE notification skipped: replyToken and userId are both missing");
  logEvent("line_delivery", { channel: "none", outcome: "failed", reason: "missing_destination" });
  return false;
}
__name(notifyLine, "notifyLine");
async function showLoadingAnimation(userId, seconds, env) {
  const roundedSeconds = Math.min(60, Math.max(5, Math.round(seconds / 5) * 5));
  try {
    const res = await fetch("https://api.line.me/v2/bot/chat/loading/start", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`
      },
      body: JSON.stringify({ chatId: userId, loadingSeconds: roundedSeconds }),
      signal: AbortSignal.timeout(3e3)
    });
    if (!res.ok) {
      const bodyText = await res.text().catch(() => "");
      console.error(`showLoadingAnimation: HTTP ${res.status} ${bodyText.slice(0, 200)}`);
      logEvent("loading", { outcome: "failed", status: res.status });
    } else {
      logEvent("loading", { outcome: "started", seconds: roundedSeconds });
    }
  } catch (e) {
    console.error("showLoadingAnimation failed:", e);
    logEvent("loading", { outcome: "failed", error: formatError(e) });
  }
}
__name(showLoadingAnimation, "showLoadingAnimation");
function getFixedCommandReply(text) {
  if (["\u4F7F\u3044\u65B9", "\u30D8\u30EB\u30D7"].includes(text)) return "\u98DF\u4E8B\u5199\u771F\u3092\u9001\u308B\u3068\u3001\u6599\u7406\u3068\u30AB\u30ED\u30EA\u30FC\u3092\u8A18\u9332\u3057\u307E\u3059\u3002";
  return null;
}
__name(getFixedCommandReply, "getFixedCommandReply");
export {
  DailyLimitCounter,
  index_default as default
};
//# sourceMappingURL=index.js.map
