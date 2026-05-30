let chatLog, chatInput;
let onAfterChat = null;

export function initChat(log, input) {
  chatLog = log;
  chatInput = input;
}

export function setAfterChat(fn) {
  onAfterChat = fn;
}

function renderMarkdown(text) {
  let html = text;

  // code blocks (before inline)
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, "<pre><code>$2</code></pre>");

  // inline code
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");

  // bold and italic
  html = html.replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>");
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");

  // headers
  html = html.replace(/^### (.+)$/gm, "<h4>$1</h4>");
  html = html.replace(/^## (.+)$/gm, "<h3>$1</h3>");
  html = html.replace(/^# (.+)$/gm, "<h2>$1</h2>");

  // unordered lists
  html = html.replace(/^[\s]*[-*+] (.+)$/gm, "<li>$1</li>");
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, "<ul>$1</ul>");

  // ordered lists
  html = html.replace(/^[\s]*\d+\. (.+)$/gm, "<li>$1</li>");

  // links and images
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img alt="$1" src="$2" />');
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a target="_blank" href="$2">$1</a>');

  // blockquote
  html = html.replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>");

  // horizontal rule
  html = html.replace(/^---$/gm, "<hr>");

  // paragraphs: double newlines
  html = html.replace(/\n\n+/g, "</p><p>");
  html = "<p>" + html + "</p>";

  // clean up empty paragraphs and whitespace
  html = html.replace(/<p>\s*<\/p>/g, "");
  html = html.replace(/\n/g, "<br>");

  return html;
}

function addMessage(role, text) {
  const item = document.createElement("div");
  item.className = `message ${role}`;
  item.innerHTML = renderMarkdown(text);
  chatLog.appendChild(item);
  chatLog.scrollTop = chatLog.scrollHeight;
  return item;
}

export async function sendChat(event) {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;
  chatInput.value = "";
  addMessage("user", message);

  const agentItem = addMessage("agent", "…");

  try {
    const token = localStorage.getItem("momentum_token");
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers,
      body: JSON.stringify({ message }),
    });

    if (response.status === 401) {
      localStorage.removeItem("momentum_token");
      window.location.href = "/login.html";
      return;
    }

    if (!response.ok) {
      const err = await response.json();
      agentItem.textContent = err.error || "请求失败";
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let fullText = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try {
            const parsed = JSON.parse(line.slice(6));
            if (parsed.error) {
              fullText = parsed.error;
            } else if (parsed.chunk) {
              fullText += parsed.chunk;
            }
            agentItem.textContent = fullText || "…";
            chatLog.scrollTop = chatLog.scrollHeight;
          } catch {
            // skip malformed lines
          }
        }
      }
    }
  } catch (err) {
    agentItem.textContent = `连接失败：${err.message}`;
  }

  if (onAfterChat) await onAfterChat();
}

export async function sendToAgent(message) {
  addMessage("user", message);

  const agentItem = addMessage("agent", "…");

  try {
    const token = localStorage.getItem("momentum_token");
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers,
      body: JSON.stringify({ message }),
    });

    if (response.status === 401) {
      localStorage.removeItem("momentum_token");
      window.location.href = "/login.html";
      return;
    }

    if (!response.ok) {
      const err = await response.json();
      agentItem.textContent = err.error || "请求失败";
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let fullText = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try {
            const parsed = JSON.parse(line.slice(6));
            if (parsed.error) {
              fullText = parsed.error;
            } else if (parsed.chunk) {
              fullText += parsed.chunk;
            }
            agentItem.textContent = fullText || "…";
            chatLog.scrollTop = chatLog.scrollHeight;
          } catch {
            // skip malformed lines
          }
        }
      }
    }
  } catch (err) {
    agentItem.textContent = `连接失败：${err.message}`;
  }

  if (onAfterChat) await onAfterChat();
}
