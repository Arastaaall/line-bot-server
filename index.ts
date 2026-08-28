// index.ts
// 【修正】未使用だったDeno向けimportを削除。署名検証はWeb Crypto API (crypto.subtle) を
// 使っており、この行は実際には参照されていなかった。Cloudflare Workers環境では
// deno.land のURL importはビルド時に問題を起こしうるため削除。

export interface Env {
  LINE_CHANNEL_SECRET: string;
  LINE_CHANNEL_ACCESS_TOKEN: string;
  RENDER_URL: string;
  INTERNAL_SECRET: string;
  CF_ACCOUNT_ID: string;
  CF_API_TOKEN: string;
  DAILY_LIMIT_KV: KVNamespace; // webhookEventIdの重複排除にのみ使用（Daily LimitはDurable Object化）
  DAILY_LIMIT_DO: DurableObjectNamespace; // 【追加】Daily Limitの原子的カウント用
  WORKERS_AI_MODEL?: string; // デフォルト: @cf/meta/llama-3.1-8b-instruct
  DAILY_CHAT_LIMIT?: string; // デフォルト: "20"
}

// --- Daily Limit用 Durable Object ---
// 【追加】KVの「GET→+1→PUT」は同時アクセス時に競合する（doc記載の通り）。
// Durable Objectは同一IDに対するfetch呼び出しを単一インスタンス内で直列実行するため、
// ここでのstorage.get/putは他リクエストと競合しない（真に原子的）。
// ユーザーID×日付ごとに1つのインスタンスを割り当てる設計。
export class DailyLimitCounter {
  state: DurableObjectState;

  constructor(state: DurableObjectState, _env: Env) {
    this.state = state;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const limit = parseInt(url.searchParams.get("limit") || "20");

    // Durable Object内は直列実行が保証されるため、この read-modify-write は安全
    let count = (await this.state.storage.get<number>("count")) || 0;

    if (count >= limit) {
      return Response.json({ allowed: false, count });
    }

    count += 1;
    await this.state.storage.put("count", count);

    // 初回アクセス時のみ、24時間後にアラームをセットしてカウントをリセットする
    const existingAlarm = await this.state.storage.getAlarm();
    if (existingAlarm === null) {
      await this.state.storage.setAlarm(Date.now() + 86400 * 1000);
    }

    return Response.json({ allowed: true, count });
  }

  async alarm(): Promise<void> {
    // 1日経過後、このユーザー×日付インスタンスのカウントをリセットする
    await this.state.storage.delete("count");
  }
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const url = new URL(request.url);
    if (url.pathname === "/webhook") {
      return handleWebhook(request, env, ctx);
    }
    
    return new Response("Not Found", { status: 404 });
  },
};

async function handleWebhook(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const body = await request.text();
  const signature = request.headers.get("X-Line-Signature") || "";

  // 1. LINE署名検証
  if (!await verifyLineSignature(body, signature, env.LINE_CHANNEL_SECRET)) {
    return new Response("Invalid Signature", { status: 403 });
  }

  const payload = JSON.parse(body);
  const events: any[] = payload.events || [];

  // 2. イベント処理 (並列処理せず順次処理して稳定性確保、またはPromise.all)
  // ここでは簡易的に順次処理するが、大量イベント時は ctx.waitUntil を使う
  for (const event of events) {
    ctx.waitUntil(processEvent(event, env));
  }

  return new Response("OK", { status: 200 });
}

async function processEvent(event: any, env: Env) {
  const eventId = event.webhookEventId;
  
  // 3. 重複配信排除 (KV使用)
  if (await isDuplicateEvent(eventId, env)) {
    return;
  }

  const type = event.type;
  const replyToken = event.replyToken;
  const userId = event.source.userId;

  if (type === "follow") {
    await replyToLine(replyToken, "友だち追加ありがとうございます！\n食事管理Botです。写真かテキストで食事内容を教えてくださいね。", env);
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
      // 画像は即Renderへプロキシ
      const ok = await proxyToRender(env, {
        endpoint: "/internal/image",
        payload: {
          user_id: userId,
          reply_token: replyToken,
          message_id: event.message.id
        }
      });
      if (!ok) {
        // Renderに届かなければreply_tokenは誰にも使われず、ユーザーは無反応のまま放置される。
        // Workers側から最低限のエラー通知だけは返す。
        await replyToLine(replyToken, "写真の処理に失敗しました。恐れ入りますが、もう一度送ってください。", env);
      }
    }
  }
}

async function handleTextMessage(event: any, env: Env) {
  const text = event.message.text.trim();
  const userId = event.source.userId;
  const replyToken = event.replyToken;

  // 4. 固定コマンドのローカル処理
  // 【修正】「リセット」はユーザーの状態(status)をRenderのSheets側で書き換える必要があるため、
  // Workers側で即答してはいけない。以前はここで「リセットしました」とローカル返信していたが、
  // 実際にはRender側の状態は一切変わっておらず、ユーザーへの案内が嘘になっていた。
  // ここではWorkers AIの判定も経由せず、直接Renderへ転送する（intentは指定しない＝
  // Render側の固定コマンド判定に委ねる）。
  if (text === "リセット") {
    const ok = await proxyToRender(env, {
      endpoint: "/internal/text",
      payload: {
        user_id: userId,
        reply_token: replyToken,
        text: text,
        intent: null,
      }
    });
    if (!ok) {
      await replyToLine(replyToken, "リセット処理に失敗しました。恐れ入りますが、もう一度お試しください。", env);
    }
    return;
  }

  // 「使い方」等、サーバー状態に依存しない完全に静的な返信のみここでローカル処理する
  const fixedReply = getFixedCommandReply(text);
  if (fixedReply) {
    await replyToLine(replyToken, fixedReply, env);
    return;
  }

  // 5. Workers AI による Intent 判定
  let intent = "meal_add"; // デフォルトは安全側（食事記録）
  let replyText = "";
  let aiSuccess = false;

  try {
    const aiResult = await classifyWithWorkersAI(text, env);
    if (aiResult && aiResult.intent) {
      intent = aiResult.intent;
      replyText = aiResult.reply || "";
      aiSuccess = true;
    }
  } catch (e) {
    console.error("Workers AI Error:", e);
    // AI失敗時はフォールバック（intentはデフォルトのmeal_addのままRenderへ）
  }

  // 6. Intent 分岐
  if (intent === "chat" && aiSuccess) {
    // 雑談判定時: Daily Limit チェック
    const limitOk = await checkAndIncrementDailyLimit(userId, env);
    if (!limitOk) {
      await replyToLine(replyToken, "雑談機能は1日の利用回数が制限されています。食事の記録なら無制限でご利用いただけます。", env);
      return;
    }
    await replyToLine(replyToken, replyText || "こんにちは！", env);
  } else {
    // meal_add / meal_correction / AI失敗時 -> Render へプロキシ
    const ok = await proxyToRender(env, {
      endpoint: "/internal/text",
      payload: {
        user_id: userId,
        reply_token: replyToken,
        text: text,
        intent: intent // Render側でGeminiのIntent判定をスキップするために渡す
      }
    });
    if (!ok) {
      await replyToLine(replyToken, "処理に失敗しました。恐れ入りますが、もう一度送ってください。", env);
    }
  }
}

// --- Helper Functions ---

async function verifyLineSignature(body: string, signature: string, secret: string): Promise<boolean> {
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

async function isDuplicateEvent(eventId: string, env: Env): Promise<boolean> {
  if (!eventId) return false;
  const key = `dup:${eventId}`;
  const existing = await env.DAILY_LIMIT_KV.get(key);
  if (existing) return true;
  
  // 10秒間だけ保持して重複を防ぐ
  await env.DAILY_LIMIT_KV.put(key, "1", { expirationTtl: 10 });
  return false;
}

async function classifyWithWorkersAI(text: string, env: Env): Promise<any> {
  const model = env.WORKERS_AI_MODEL || "@cf/meta/llama-3.1-8b-instruct";
  const url = `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/ai/run/${model}`;
  
  const prompt = `
You are a diet assistant. Classify the user input into one of: "chat", "meal_add", "meal_correction".
- "chat": General conversation, greetings, questions not about specific meal logging.
- "meal_add": User wants to log a new meal.
- "meal_correction": User wants to correct the previous meal log.
If "chat", provide a friendly reply in Japanese.
If "meal_add" or "meal_correction", set reply to null.
Output ONLY valid JSON: {"intent": "...", "reply": "..."}
Input: "${text.replace(/"/g, '\\"')}"
`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.CF_API_TOKEN}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ prompt })
  });

  if (!response.ok) throw new Error("AI API Error");
  
  const result = await response.json();
  // Workers AI のレスポンス構造からテキスト抽出
  const rawText = result.result?.response || "";
  
  // JSON抽出
  const match = rawText.match(/\{[\s\S]*\}/);
  if (match) {
    return JSON.parse(match[0]);
  }
  throw new Error("Invalid JSON from AI");
}

async function checkAndIncrementDailyLimit(userId: string, env: Env): Promise<boolean> {
  // 【修正】KVでのGET→+1→PUTは同時アクセス時に競合する（例: 同一ユーザーから
  // ほぼ同時に2通来ると両方とも同じcurrent値を読み、上限を超えて許可されてしまう）。
  // ユーザーID×日付ごとに1つのDurable Objectインスタンスへ処理を委譲することで、
  // チェックと増加を原子的に行う。
  const limit = env.DAILY_CHAT_LIMIT || "20";
  const today = new Date().toISOString().split('T')[0]; // YYYY-MM-DD
  const id = env.DAILY_LIMIT_DO.idFromName(`${userId}:${today}`);
  const stub = env.DAILY_LIMIT_DO.get(id);

  const response = await stub.fetch(`https://daily-limit/check?limit=${limit}`);
  const result = await response.json<{ allowed: boolean; count: number }>();
  return result.allowed;
}

async function proxyToRender(env: Env, data: { endpoint: string, payload: any }): Promise<boolean> {
  // 【修正】以前はfetchの結果を一切見ておらず、Renderが500を返しても
  // Workers側は何も検知せず、LINEユーザーには永遠に返信が届かなかった。
  // ステータスコードのチェック・タイムアウト・失敗時のログを追加し、
  // 呼び出し側が失敗を検知してユーザーへフォールバック通知できるようにする。
  const url = `${env.RENDER_URL}${data.endpoint}`;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Secret": env.INTERNAL_SECRET
      },
      body: JSON.stringify(data.payload),
      signal: AbortSignal.timeout(20000), // 20秒でタイムアウト（Render側が固まった場合の保険）
    });

    if (!response.ok) {
      const bodyText = await response.text().catch(() => "");
      console.error(`proxyToRender: Render returned ${response.status} for ${data.endpoint}: ${bodyText}`);
      return false;
    }
    return true;
  } catch (e) {
    console.error(`proxyToRender: request to ${data.endpoint} failed:`, e);
    return false;
  }
}

async function replyToLine(replyToken: string, message: string, env: Env) {
  await fetch("https://api.line.me/v2/bot/message/reply", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`
    },
    body: JSON.stringify({
      replyToken,
      messages: [{ type: "text", text: message }]
    })
  });
}

function getFixedCommandReply(text: string): string | null {
  // 【修正】「リセット」はここから削除し、handleTextMessage側でRenderへ直接転送するようにした。
  if (["使い方", "ヘルプ"].includes(text)) return "食事写真を送ると、料理とカロリーを記録します。";
  return null;
}