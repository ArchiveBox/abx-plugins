import { verifyReceipt } from "./verify.mjs";
if (new URLSearchParams(location.search).has("compact"))
  document.body.classList.add("compact");
const status = document.querySelector("#status"),
  details = document.querySelector("#details");
async function verify(receipt, response) {
  status.textContent = "Checking signature and archived bytes…";
  status.className = "";
  details.replaceChildren();
  document.querySelector("#content").hidden = true;
  try {
    const trust = await (await fetch("trust.json")).json();
    const result = await verifyReceipt(receipt, response, trust.publicKey);
    status.textContent = "Verified · signature and archived response match";
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
      result.body,
    );
    document.querySelector("#content").hidden = false;
  } catch (error) {
    status.textContent = "Verification failed: " + error.message;
    status.className = "failed";
  }
}
async function files(list) {
  const entries = [...list];
  const receipt = entries.find((f) => f.name === "receipt.json"),
    response = entries.find((f) => f.name === "response.http");
  if (!receipt || !response) {
    status.textContent = "Select both receipt.json and response.http.";
    return;
  }
  await verify(
    JSON.parse(await receipt.text()),
    new Uint8Array(await response.arrayBuffer()),
  );
}
document.querySelector("#files").addEventListener("change", (e) =>
  files(e.target.files).catch((e) => {
    status.textContent = e.message;
    status.className = "failed";
  }),
);
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault();
  files(e.dataTransfer.files).catch((e) => {
    status.textContent = e.message;
    status.className = "failed";
  });
});
try {
  const [receipt, response] = await Promise.all([
    fetch("receipt.json"),
    fetch("response.http"),
  ]);
  if (receipt.ok && response.ok)
    await verify(
      await receipt.json(),
      new Uint8Array(await response.arrayBuffer()),
    );
} catch {
  status.textContent =
    "Select receipt.json and response.http to verify locally.";
}
