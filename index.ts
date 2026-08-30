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
  const replyToken = event.replyToken;

  // 【追加】ctx.waitUntil(processEvent(...))の中で例外が起きると、
  // メインのfetchハンドラは既に200 OKを返した後なので、Cloudflareの
  // Errorsカウントにも上がらず、ユーザーには何も届かないまま静かに失敗する
  // （今回のexpirationTtlのバグがまさにこれだった）。
  // 同種の問題を今後すぐ気づけるよう、ここで丸ごと捕捉してログに残し、
  // 可能な場合はユーザーにも最低限の通知を返す。
  try {
    await processEventInner(event, env);
  } catch (e) {
    console.error("processEvent failed:", e);
    if (replyToken) {
      await replyToLine(replyToken, "処理中に問題が起きました。少し時間をおいて、もう一度お試しください。", env).catch(() => {});
    }
  }
}

async function processEventInner(event: any, env: Env) {
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
      // 【追加】画像はRender側の解析（最大約28秒）を待つ必要があるため、
      // ここWorkers側で即座にローディングアニメーションを出しておく
      // （Render側の show_loading_sync 呼び出しより一手早く表示できる）。
      await showLoadingAnimation(userId, 30, env);
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

  // 【追加】ここから先はGemini/Workers AIの呼び出しが絡み、数秒〜20秒程度かかりうる。
  // 以前はここでLINEの「入力中...」ローディングアニメーションを一切出しておらず、
  // ユーザーからは「本当に動いているのか」が分からなかった。
  // Workers側（ユーザーの操作から一番近い場所）で先に表示しておくことで、
  // この後Renderに処理を渡してからの待ち時間も含めてカバーする
  // （LINEは実際に返信/Pushが送られると自動でアニメーションを終了する）。
  await showLoadingAnimation(userId, 30, env);

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
    const limitResult = await checkAndIncrementDailyLimit(userId, env);
    if (!limitResult.allowed) {
      await replyToLine(
        replyToken,
        `雑談機能は1日の利用回数（${limitResult.limit}回）に達しました。食事の記録なら無制限でご利用いただけます。`,
        env
      );
      return;
    }
    // 【追加】「視覚的に見えるようにしてほしい」との要望に対応。
    // 毎回の雑談返信の末尾に、本日あと何回使えるかを一言添える。
    const remaining = Math.max(limitResult.limit - limitResult.count, 0);
    const counterNote = `\n\n（本日の雑談: 残り${remaining}/${limitResult.limit}回）`;
    await replyToLine(replyToken, (replyText || "こんにちは！") + counterNote, env);
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
  try {
    const existing = await env.DAILY_LIMIT_KV.get(key);
    if (existing) return true;

    // 【修正】Cloudflare KVのexpirationTtlは現在60秒以上でないと400エラーになる
    // （以前は10秒を指定しており、KV PUTのたびに例外を投げていた）。
    // 重複排除としては60秒あれば十分（LINEの再送は通常もっと早いタイミングで来る）。
    await env.DAILY_LIMIT_KV.put(key, "1", { expirationTtl: 60 });
    return false;
  } catch (e) {
    // 【追加】KV側で何か起きても、ここで処理全体を止めない（fail-open）。
    // 今回のTTLエラーのように、ここで例外を投げるとprocessEvent全体が
    // ctx.waitUntil内で静かに死に、ユーザーには何も届かなくなってしまう。
    // 重複排除に失敗しても「重複ではない」として処理を続行する方が実害が小さい。
    console.error("isDuplicateEvent failed, treating as non-duplicate:", e);
    return false;
  }
}

async function classifyWithWorkersAI(text: string, env: Env): Promise<any> {
  // 【軽量化】判定用途は速度優先でよいため、既定モデルを低遅延版(-fast)に変更。
  // 通常の8bモデルより応答が速く、失敗率が下がることで legacy(Render側でGeminiに
  // intentから判定させ直す)経路に落ちる頻度そのものを減らせる。
  const model = env.WORKERS_AI_MODEL || "@cf/meta/llama-3.1-8b-instruct-fast";
  const url = `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/ai/run/${model}`;
  
  const prompt = `
You are a diet assistant. Classify the user input into one of: "chat", "meal_add", "meal_correction".
- "chat": General conversation, greetings, questions not about specific meal logging.
- "meal_add": User wants to log a new meal.
- "meal_correction": User wants to correct the previous meal log.
If "chat", provide a friendly reply in Japanese in the "reply" field.
If "meal_add" or "meal_correction", set "reply" to an empty string.
Input: "${text.replace(/"/g, '\\"')}"
`;

  // 【修正】以前は自由記述のテキストから正規表現で { ... } を抜き出してJSON.parseしていたが、
  // モデル（特に-fast版）が指示に従わずコードフェンス（```json ... ```）や前置きの文章を
  // 付けて返すことがあり、そのままJSON.parseが失敗していた（今回報告されたエラーそのもの）。
  // Workers AIのJSON Mode（response_format）でスキーマを強制し、モデル側で
  // 構造化されたJSONしか返させないようにする。これにより解析失敗そのものを減らす。
  // 【修正】以前はタイムアウト(AbortError)もHTTPエラーも「AI API Error」という
  // 中身のない文言でしか分からず、実際に何が起きたのかログから判断できなかった。
  // ステータスコード・レスポンス本文・タイムアウトかどうかを区別して残す。
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.CF_API_TOKEN}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        prompt,
        response_format: {
          type: "json_schema",
          json_schema: {
            type: "object",
            properties: {
              intent: { type: "string", enum: ["chat", "meal_add", "meal_correction"] },
              reply: { type: "string" },
            },
            required: ["intent", "reply"],
          },
        },
      }),
      signal: AbortSignal.timeout(4000),
    });
  } catch (e) {
    if (e instanceof Error && e.name === "TimeoutError") {
      throw new Error("Workers AI タイムアウト（4秒以内に応答なし）");
    }
    throw new Error(`Workers AI fetch失敗: ${(e as Error).message}`);
  }

  if (!response.ok) {
    const bodyText = await response.text().catch(() => "");
    throw new Error(`AI API Error: HTTP ${response.status} ${bodyText.slice(0, 300)}`);
  }
  
  const result = await response.json();
  // Workers AI のレスポンス構造からテキスト抽出
  const rawText: string = result.result?.response || "";

  // 【修正】JSON Modeを使っていてもモデルがコードフェンスを付けてくることがあるため、
  // 念のため ```json / ``` を取り除いてから解析する（多層の防御）。
  const cleaned = rawText.replace(/```json|```/g, "").trim();

  // JSON抽出
  const match = cleaned.match(/\{[\s\S]*\}/);
  if (!match) {
    throw new Error(`Invalid JSON from AI（本文が見つからない）: ${rawText.slice(0, 200)}`);
  }
  try {
    return JSON.parse(match[0]);
  } catch (e) {
    // 【修正】以前はJSON.parseの失敗理由が分からず、"Workers AI Error"としか
    // ログに残らなかった。実際に届いた本文を一緒に残すことで、次に同じ失敗が
    // 起きたときに原因（コードフェンス、途中で切れた等）をすぐ判断できるようにする。
    throw new Error(`Invalid JSON from AI（parse失敗: ${(e as Error).message}）: ${match[0].slice(0, 200)}`);
  }
}

async function checkAndIncrementDailyLimit(userId: string, env: Env): Promise<{ allowed: boolean; count: number; limit: number }> {
  // 【修正】KVでのGET→+1→PUTは同時アクセス時に競合する（例: 同一ユーザーから
  // ほぼ同時に2通来ると両方とも同じcurrent値を読み、上限を超えて許可されてしまう）。
  // ユーザーID×日付ごとに1つのDurable Objectインスタンスへ処理を委譲することで、
  // チェックと増加を原子的に行う。
  const limit = parseInt(env.DAILY_CHAT_LIMIT || "20");
  const today = new Date().toISOString().split('T')[0]; // YYYY-MM-DD
  const id = env.DAILY_LIMIT_DO.idFromName(`${userId}:${today}`);
  const stub = env.DAILY_LIMIT_DO.get(id);

  const response = await stub.fetch(`https://daily-limit/check?limit=${limit}`);
  const result = await response.json<{ allowed: boolean; count: number }>();
  // 【追加】呼び出し側で「本日あと何回使えるか」を表示できるよう、countとlimitも返す。
  return { allowed: result.allowed, count: result.count, limit };
}

async function proxyToRender(env: Env, data: { endpoint: string, payload: any }): Promise<boolean> {
  // 【修正】以前はfetchの結果を一切見ておらず、Renderが500を返しても
  // Workers側は何も検知せず、LINEユーザーには永遠に返信が届かなかった。
  // ステータスコードのチェック・タイムアウト・失敗時のログを追加し、
  // 呼び出し側が失敗を検知してユーザーへフォールバック通知できるようにする。
  const url = `${env.RENDER_URL}${data.endpoint}`;
  try {
    // 【重要・main.py側の変更とセット】以前はRender側が「LINEへの返信/Push完了まで」
    // このHTTPリクエストへの応答を返さない実装だったため、Renderの処理時間が
    // 伸びる（Geminiのフォールバック連鎖など）とここのタイムアウト値と直接衝突し、
    // Workers側が「失敗」と誤判定してエラーメッセージを二重送信しかねなかった。
    // main.py側を「受け取ったら即座に200を返し、処理はバックグラウンドで続行する」
    // 方式に変更したことで、Renderの応答はほぼ即時になる。そのためここのタイムアウトは
    // 「Renderへ処理を渡せたかどうか」だけを見る短い値（8秒）に短縮する。
    // ※ main.py側の /internal/text, /internal/image を非同期化する変更と必ずセットでデプロイすること。
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Secret": env.INTERNAL_SECRET
      },
      body: JSON.stringify(data.payload),
      signal: AbortSignal.timeout(8000),
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

async function showLoadingAnimation(userId: string, seconds: number, env: Env): Promise<void> {
  // 【追加】LINEの「入力中...」ローディングアニメーションを表示するAPI。
  // これは特定のreply_tokenではなくユーザー(chatId)単位の表示であり、
  // 実際にメッセージ（reply/push）が送られると自動的に終了する。
  // loadingSecondsは5〜60の間で5刻みである必要があるため、丸めておく。
  const roundedSeconds = Math.min(60, Math.max(5, Math.round(seconds / 5) * 5));
  try {
    const res = await fetch("https://api.line.me/v2/bot/chat/loading/start", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`
      },
      body: JSON.stringify({ chatId: userId, loadingSeconds: roundedSeconds }),
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) {
      const bodyText = await res.text().catch(() => "");
      console.error(`showLoadingAnimation: HTTP ${res.status} ${bodyText.slice(0, 200)}`);
    }
  } catch (e) {
    // 【重要】あくまで見た目の補助機能なので、ここで失敗しても本処理は止めない。
    console.error("showLoadingAnimation failed:", e);
  }
}

function getFixedCommandReply(text: string): string | null {
  // 【修正】「リセット」はここから削除し、handleTextMessage側でRenderへ直接転送するようにした。
  if (["使い方", "ヘルプ"].includes(text)) return "食事写真を送ると、料理とカロリーを記録します。";
  return null;
}