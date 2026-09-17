import { verifyReceipt, TRUSTED_PUBLIC_KEY } from "./verify.mjs";
if (new URLSearchParams(location.search).has("compact"))
  document.body.classList.add("compact");
const status = document.querySelector("#status"),
  details = document.querySelector("#details");
let generation = 0;
function reset(message) {
  status.textContent = message;
  status.className = "";
  details.replaceChildren();
  document.querySelector("#content").hidden = true;
  document.querySelector("#body").textContent = "";
}
async function verify(receipt, response, current) {
  if (current !== generation) return;
  reset("Checking signature and archived bytes…");
  try {
    const result = await verifyReceipt(receipt, response, TRUSTED_PUBLIC_KEY);
    if (current !== generation) return;
    status.textContent = document.body.classList.contains("compact")
      ? "Verified response"
      : "Verified · signature and archived response match";
    status.className = "verified";
    for (const [label, value] of Object.entries({
      "Authenticated server": result.server_name,
      "Verifier time": new Date(result.time * 1000).toISOString(),
      "HTTP status": result.status,
      "Authenticated bytes": response.length.toLocaleString(),
      "Receipt bytes": JSON.stringify(receipt).length,
      Commitment: result.hash,
    })) {
      const dt = document.createElement("dt"),
        dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value;
      details.append(dt, dd);
    }
    document.querySelector("#body").textContent = new TextDecoder().decode(
      result.body
    );
    document.querySelector("#content").hidden = false;
  } catch (error) {
    if (current !== generation) return;
    status.textContent = "Verification failed: " + error.message;
    status.className = "failed";
  }
}
async function files(list) {
  const current = ++generation;
  reset("Reading capture files…");
  const entries = [...list];
  const receipt = entries.find((f) => f.name === "receipt.json"),
    response = entries.find((f) => f.name === "response.http");
  if (!receipt || !response) {
    reset("Select both receipt.json and response.http.");
    return;
  }
  try {
    await verify(
      JSON.parse(await receipt.text()),
      new Uint8Array(await response.arrayBuffer()),
      current
    );
  } catch (error) {
    if (current !== generation) return;
    reset("Could not read capture files: " + error.message);
    status.className = "failed";
  }
}
document
  .querySelector("#files")
  .addEventListener("change", (e) => files(e.target.files));
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault();
  files(e.dataTransfer.files);
});
const initial = generation;
try {
  const [receipt, response] = await Promise.all([
    fetch("receipt.json"),
    fetch("response.http"),
  ]);
  if (receipt.ok && response.ok)
    await verify(
      await receipt.json(),
      new Uint8Array(await response.arrayBuffer()),
      initial
    );
} catch {
  if (generation === initial)
    reset("Select receipt.json and response.http to verify locally.");
}
