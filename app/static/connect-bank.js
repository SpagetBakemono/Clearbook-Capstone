/* "Connect a bank": opens Plaid Link with the user's own keys; on success
   the server creates the accounts and pulls their history, then we go to
   the Dashboard. Any [data-connect-bank] button on the page works. */
(function () {
  "use strict";
  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Something went wrong (" + res.status + ").");
    return data;
  }
  document.querySelectorAll("[data-connect-bank]").forEach(function (btn) {
    const status = document.querySelector(btn.dataset.status || "#connect-status");
    const say = function (text, isError) {
      if (!status) return;
      status.textContent = text;
      status.classList.toggle("is-error", !!isError);
    };
    btn.addEventListener("click", async function () {
      btn.disabled = true;
      say("Opening Plaid…");
      try {
        const { link_token } = await postJson("/plaid/create-link-token");
        Plaid.create({
          token: link_token,
          onSuccess: async function (public_token) {
            say("Adding your accounts and their history… this takes a few seconds.");
            try {
              await postJson("/plaid/connect-bank", { public_token: public_token });
              window.location.href = "/";
            } catch (err) { say(err.message, true); btn.disabled = false; }
          },
          onExit: function (err) {
            say(err ? (err.display_message || err.error_message || "Plaid closed with an error.") : "", !!err);
            btn.disabled = false;
          },
        }).open();
      } catch (err) { say(err.message, true); btn.disabled = false; }
    });
  });
})();
