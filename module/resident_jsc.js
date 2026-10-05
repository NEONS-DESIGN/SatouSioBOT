// 抽出ワーカーに常駐し、YouTube の JS チャレンジ (n / sig) を解く Deno 側の処理。
// 標準入力から 1 行 1 JSON の要求を受け取り、1 行 1 JSON で応答する。
// 標準入力が閉じたら (ワーカーの終了) 読み込みが終わり、プロセスも終了する。
//
// 要求:
//   {type: "init", code}  解読スクリプト (yt-dlp-ejs の lib + core) を評価する → {type: "ok"}
//   {type: "solve", player_url, requests, player?, preprocessed_player?, return_preprocessed?}
//     player / preprocessed_player を省くと保持済みのプレイヤーを使う。保持していなければ {type: "missing"}
//     それ以外は yt-dlp-ejs の jsc() の出力 ({type: "result", responses, preprocessed_player?})
//   失敗時は {type: "error", error}

// 標準出力は応答専用にする (評価したスクリプトが console.log を使っても、応答の行とずれないよう標準エラーへ回す)
console.log = console.info = console.debug = console.error;

// 前処理済みプレイヤーを保持する版数 (1 版あたり約 4MB。別の版が必要になると Python 側がプロセスごと起動し直す)
const PLAYER_KEEP = 1;

// player_url -> 前処理済みプレイヤー (Map の挿入順を古い順として使う)
const players = new Map();
let solver = null;

/** 前処理済みプレイヤーを保持する (古い版から捨てる) */
function remember(playerUrl, preprocessed) {
	players.delete(playerUrl);
	players.set(playerUrl, preprocessed);
	while (players.size > PLAYER_KEEP) {
		players.delete(players.keys().next().value);
	}
}

/** solve 要求を jsc() の入力に変換して解く。プレイヤーが無ければ missing を返す */
function solve(message) {
	if (solver === null) {
		return { type: "error", error: "solver is not initialized" };
	}
	const requests = message.requests;
	let input;
	if (typeof message.player === "string") {
		input = { type: "player", player: message.player, requests, output_preprocessed: true };
	} else {
		const preprocessed = message.preprocessed_player ?? players.get(message.player_url);
		if (typeof preprocessed !== "string") {
			return { type: "missing" };
		}
		input = { type: "preprocessed", preprocessed_player: preprocessed, requests };
	}
	const output = solver(input);
	const preprocessed = output.preprocessed_player ?? input.preprocessed_player;
	if (output.type === "result" && typeof preprocessed === "string") {
		remember(message.player_url, preprocessed);
	}
	if (!message.return_preprocessed) {
		delete output.preprocessed_player;
	}
	return output;
}

/** 1 行の要求を処理して応答のオブジェクトを返す (例外は error 応答にする) */
function respond(line) {
	try {
		const message = JSON.parse(line);
		switch (message.type) {
			case "init":
				(0, eval)(message.code);
				solver = globalThis.jsc;
				if (typeof solver !== "function") {
					solver = null;
					return { type: "error", error: "jsc is not defined by the solver script" };
				}
				return { type: "ok" };
			case "solve":
				return solve(message);
			default:
				return { type: "error", error: `unknown message type: ${message.type}` };
		}
	} catch (error) {
		return { type: "error", error: error instanceof Error ? `${error.message}\n${error.stack}` : `${error}` };
	}
}

const decoder = new TextDecoder();
const encoder = new TextEncoder();
const writer = Deno.stdout.writable.getWriter();
// 改行が来るまでの断片。プレイヤー (約 4MB) は複数の読み込みに分かれて届く
let parts = [];
for await (const chunk of Deno.stdin.readable) {
	const text = decoder.decode(chunk, { stream: true });
	let start = 0;
	let newline;
	while ((newline = text.indexOf("\n", start)) >= 0) {
		parts.push(text.slice(start, newline));
		const line = parts.join("");
		parts = [];
		start = newline + 1;
		if (line.trim()) {
			await writer.write(encoder.encode(JSON.stringify(respond(line)) + "\n"));
		}
	}
	parts.push(text.slice(start));
}
