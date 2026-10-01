// Mail tab for the Hermes dashboard: accounts, sign-in, notifications and
// the triage model. Plain IIFE, no build step.
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useToast } = SDK.hooks;
  const {
    Card, CardHeader, CardTitle, CardContent, Badge, Button, Checkbox, ConfirmDialog,
    Input, Label, Select, SelectOption, Toast,
  } = SDK.components;

  const API = "/api/plugins/hermes-mail";
  const TEXTAREA = "flex w-full border border-midground/15 bg-background/40 px-3 py-2 font-courier text-sm resize-y";
  const PROVIDERS = { microsoft: "Microsoft 365 / Outlook.com", google: "Gmail", imap: "Other IMAP server" };
  const NUMBERS = ["port", "sync_days", "poll_seconds", "cache_limit_mb", "max_part_mb"];
  const NEW_ACCOUNT = {
    provider: "microsoft", address: "", auth: "oauth", host: "", port: 993, folders: ["INBOX"],
    archive_folder: "", sync_days: 7, poll_seconds: 300, cache_limit_mb: 100, max_part_mb: 25,
  };

  // Every route answers {ok, error}. Turn an error answer into a rejection.
  function post(path, body) {
    return SDK.fetchJSON(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (data) {
      if (!data.ok) throw new Error(data.error);
      return data;
    });
  }

  function accountPath(name) {
    return "/accounts/" + encodeURIComponent(name);
  }

  // `props.children` is a function that gets the id for its control.
  function Field(props) {
    const id = React.useId();
    return h("div", { className: "flex flex-col gap-2" },
      h(Label, { htmlFor: id }, props.label),
      props.children(id),
      props.hint ? h("p", { className: "text-xs text-muted-foreground" }, props.hint) : null);
  }

  function TextField(props) {
    return h(Field, { label: props.label, hint: props.hint }, function (id) {
      return h(Input, {
        id: id,
        type: props.type || "text",
        value: props.value === null || props.value === undefined ? "" : String(props.value),
        placeholder: props.placeholder || "",
        disabled: props.disabled,
        autoComplete: props.type === "password" ? "new-password" : "off",
        onChange: function (event) { props.onChange(event.target.value); },
      });
    });
  }

  function ChoiceField(props) {
    return h(Field, { label: props.label, hint: props.hint }, function (id) {
      return h(Select, { id: id, value: props.value, disabled: props.disabled, onValueChange: props.onChange },
        props.options.map(function (option) {
          return h(SelectOption, { key: option[0], value: option[0] }, option[1]);
        }));
    });
  }

  function TextAreaField(props) {
    return h(Field, { label: props.label, hint: props.hint }, function (id) {
      return h("textarea", {
        id: id, className: TEXTAREA, rows: props.rows, value: props.value,
        onChange: function (event) { props.onChange(event.target.value); },
      });
    });
  }

  function ErrorText(props) {
    return props.text ? h("p", { className: "text-sm text-destructive" }, props.text) : null;
  }

  // State for one form: its values, a busy flag and the last error.
  function useForm(initial) {
    const [values, setValues] = useState(initial);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);

    function set(key) {
      return function (value) {
        setValues(function (current) {
          const next = Object.assign({}, current);
          next[key] = value;
          return next;
        });
      };
    }

    function submit(request, done) {
      setBusy(true);
      setError(null);
      request().then(function (data) {
        setBusy(false);
        done(data);
      }, function (err) {
        setBusy(false);
        setError(err.message || String(err));
      });
    }

    return { values: values, setValues: setValues, set: set, busy: busy, error: error, submit: submit };
  }

  function AccountForm(props) {
    const meta = props.meta;
    const account = props.account;
    const start = account ? account.settings : NEW_ACCOUNT;
    const form = useForm(Object.assign({}, start, { name: "", password: "", folders: (start.folders || []).join("\n") }));
    const v = form.values;
    const oauth = v.auth === "oauth";

    function setProvider(value) {
      form.setValues(Object.assign({}, v, { provider: value, auth: value === "imap" ? "password" : "oauth" }));
    }

    function save() {
      const settings = {};
      Object.keys(NEW_ACCOUNT).forEach(function (key) { settings[key] = v[key]; });
      NUMBERS.forEach(function (key) { settings[key] = Number(v[key]); });
      settings.folders = String(v.folders).split("\n").map(function (item) { return item.trim(); })
        .filter(Boolean);
      if (oauth) settings.host = "";
      const name = account ? account.name : v.name.trim();
      form.submit(function () {
        return post(accountPath(name), { settings: settings, password: oauth ? "" : v.password });
      }, function () { props.onSaved(account ? "Saved the " + name + " account." : "Added the " + name + " account."); });
    }

    const passwordHint = account && account.password === "dashboard"
      ? "A password is stored. Leave this empty to keep it."
      : account && account.password === "nix"
        ? "The NixOS password file applies while the server and the address stay the same."
        : "The service keeps the password in its state directory with mode 0600.";

    return h("div", { className: "flex flex-col gap-4" },
      account ? null : h(TextField, {
        label: "Account name", value: v.name, onChange: form.set("name"), placeholder: "university",
        hint: "Lower-case letters, digits, - and _. Mail IDs start with it.",
      }),
      h("div", { className: "grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3" },
        h(ChoiceField, {
          label: "Provider", value: v.provider, onChange: setProvider,
          options: meta.providers.map(function (item) { return [item, PROVIDERS[item] || item]; }),
        }),
        h(TextField, {
          label: "Address", value: v.address, onChange: form.set("address"), placeholder: "someone@example.com",
          hint: "The mail address, which is also the IMAP user name.",
        }),
        h(ChoiceField, {
          label: "Sign-in", value: v.auth, onChange: form.set("auth"), disabled: v.provider === "imap",
          options: [["oauth", "OAuth2 in a browser"], ["password", "Password or app password"]],
        }),
        oauth ? null : h(TextField, { label: "Password", type: "password", value: v.password, onChange: form.set("password"), hint: passwordHint }),
        h(TextField, {
          label: "IMAP host", value: oauth ? "" : v.host, onChange: form.set("host"), disabled: oauth,
          placeholder: meta.default_hosts[v.provider] || "imap.example.com",
          hint: oauth ? "An OAuth account always uses the host of its provider." : null,
        }),
        h(TextField, { label: "IMAP port", type: "number", value: v.port, onChange: form.set("port"), hint: "The service always uses TLS." }),
        h(TextField, {
          label: "Archive folder", value: v.archive_folder, onChange: form.set("archive_folder"),
          placeholder: meta.default_archive_folders[v.provider] || "Archive",
          hint: "Where mail_archive moves mail. Empty means the default of the provider.",
        }),
        h(TextField, { label: "Sync window (days)", type: "number", value: v.sync_days, onChange: form.set("sync_days") }),
        h(TextField, { label: "Full check interval (seconds)", type: "number", value: v.poll_seconds, onChange: form.set("poll_seconds") }),
        h(TextField, { label: "Text cache limit (MB)", type: "number", value: v.cache_limit_mb, onChange: form.set("cache_limit_mb") }),
        h(TextField, { label: "Largest attachment (MB)", type: "number", value: v.max_part_mb, onChange: form.set("max_part_mb") })),
      h(TextAreaField, {
        label: "Folders", rows: 3, value: v.folders, onChange: form.set("folders"),
        hint: "One folder for each line. The service waits for new mail on the first folder.",
      }),
      h(ErrorText, { text: form.error }),
      h("div", { className: "flex flex-wrap gap-2" },
        h(Button, { size: "sm", onClick: save, disabled: form.busy }, account ? "Save account" : "Add account"),
        h(Button, { size: "sm", outlined: true, onClick: props.onCancel, disabled: form.busy }, "Cancel")));
  }

  function NotifyForm(props) {
    const account = props.account;
    const notify = account.notify;
    const start = notify.current;
    const form = useForm({
      mode: start.mode, target: start.target, policy: start.policy,
      mark_read_silent: !!start.mark_read_silent, task_command: (start.task_command || []).join(" "),
    });
    const v = form.values;
    const checkboxId = React.useId();

    function save() {
      form.submit(function () { return post(accountPath(account.name) + "/notify", v); },
        function () { props.onSaved("Saved the notifications of " + account.name + "."); });
    }

    function useNix() {
      form.submit(function () { return post(accountPath(account.name) + "/notify/reset"); },
        function () { props.onSaved("The NixOS notifications of " + account.name + " apply again."); });
    }

    return h("div", { className: "flex flex-col gap-4" },
      h("p", { className: "text-xs text-muted-foreground" }, notify.dashboard
        ? "These settings replace the NixOS notifications of this account."
        : "These are the NixOS notifications. A save replaces them for this account."),
      !notify.dashboard && notify.nix.policy_error ? h(ErrorText, { text: notify.nix.policy_error }) : null,
      h("div", { className: "grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3" },
        h(ChoiceField, {
          label: "Mode", value: v.mode, onChange: form.set("mode"),
          options: [["triage", "Triage"], ["all", "All new mail"], ["none", "None"]],
          hint: "Triage lets the model select the mail to notify.",
        }),
        h(TextField, {
          label: "Target", value: v.target, onChange: form.set("target"), placeholder: "discord:1234567890",
          hint: "The Hermes send target.",
        }),
        h(TextField, {
          label: "Task command", value: v.task_command, onChange: form.set("task_command"),
          placeholder: "/run/current-system/sw/bin/my-task-helper",
          hint: "Gets the task as JSON on stdin. Empty means no tasks.",
        })),
      h("div", { className: "flex items-center gap-2" },
        h(Checkbox, {
          id: checkboxId, checked: v.mark_read_silent,
          onCheckedChange: function (value) { form.set("mark_read_silent")(value === true); },
        }),
        h(Label, { htmlFor: checkboxId }, "Mark mail as read when the triage does not notify")),
      h(TextAreaField, {
        label: "Triage policy", rows: 10, value: v.policy, onChange: form.set("policy"),
        hint: "The triage rules of this account. Empty means the default rule.",
      }),
      h(ErrorText, { text: form.error }),
      h("div", { className: "flex flex-wrap gap-2" },
        h(Button, { size: "sm", onClick: save, disabled: form.busy }, "Save notifications"),
        notify.dashboard ? h(Button, { size: "sm", outlined: true, onClick: useNix, disabled: form.busy }, "Use NixOS settings") : null,
        h(Button, { size: "sm", outlined: true, onClick: props.onCancel, disabled: form.busy }, "Cancel")));
  }

  function SignIn(props) {
    const account = props.account;
    const form = useForm({ redirect: "" });
    const [begin, setBegin] = useState(null);

    function start() {
      form.submit(function () { return post(accountPath(account.name) + "/signin"); }, setBegin);
    }

    function finish() {
      form.submit(function () {
        return post(accountPath(account.name) + "/signin/finish", { redirect: form.values.redirect.trim() });
      }, function (data) { props.onSaved(data.message); });
    }

    return h("div", { className: "flex flex-col gap-4" },
      begin
        ? h("ol", { className: "list-decimal pl-5 text-sm flex flex-col gap-1" },
          h("li", null, "Open ", h("a", { className: "underline", href: begin.url, target: "_blank", rel: "noreferrer" }, "the sign-in page"),
            " and sign in, with MFA if the account asks for it."),
          h("li", null, "The browser then goes to " + begin.redirect_uri + "/?code=…, which does not load. This is expected."),
          h("li", null, "Copy the full URL from the address bar and paste it here."))
        : h("p", { className: "text-xs text-muted-foreground" },
          "The service keeps the refresh token. Hermes never sees it."),
      begin ? h(TextField, { label: "Redirect URL", value: form.values.redirect, onChange: form.set("redirect") }) : null,
      h(ErrorText, { text: form.error }),
      h("div", { className: "flex flex-wrap gap-2" },
        begin
          ? h(Button, { size: "sm", onClick: finish, disabled: form.busy || !form.values.redirect.trim() }, "Finish sign-in")
          : h(Button, { size: "sm", onClick: start, disabled: form.busy }, "Start sign-in"),
        h(Button, { size: "sm", outlined: true, onClick: props.onCancel, disabled: form.busy }, "Cancel")));
  }

  function statusTone(account) {
    if (account.removed) return "outline";
    if (account.status === "idle" || account.status === "syncing") return "success";
    if (account.status === "starting") return "secondary";
    return "destructive";
  }

  function AccountCard(props) {
    const account = props.account;
    const editable = props.meta.editable;
    const [open, setOpen] = useState(null);
    const [busy, setBusy] = useState(false);

    function done(message) {
      setOpen(null);
      props.onChanged(message);
    }

    function reset() {
      setBusy(true);
      post(accountPath(account.name) + "/reset").then(function () {
        done(account.removed ? "Restored the " + account.name + " account." : "The NixOS settings of " + account.name + " apply again.");
      }, function (err) { props.onError(err.message); }).then(function () { setBusy(false); });
    }

    function toggle(key, label) {
      return h(Button, { size: "sm", outlined: open !== key, onClick: function () { setOpen(open === key ? null : key); } }, label);
    }

    const source = account.source === "nix" ? (account.changed ? "NixOS, changed" : "NixOS") : "Dashboard";
    const settings = account.settings || {};
    const cancel = function () { setOpen(null); };

    return h(Card, null,
      h(CardHeader, null,
        h("div", { className: "flex flex-wrap items-start justify-between gap-2" },
          h("div", { className: "flex flex-col gap-1" },
            h(CardTitle, null, account.name),
            h("p", { className: "text-xs text-muted-foreground" }, settings.address || "")),
          h("div", { className: "flex flex-wrap gap-2" },
            h(Badge, { tone: statusTone(account) }, account.removed ? "removed" : account.status),
            h(Badge, { tone: "outline" }, source),
            h(Badge, { tone: "outline" }, "notify: " + account.notify.current.mode + (account.notify.dashboard ? " (dashboard)" : ""))))),
      h(CardContent, null,
        h("div", { className: "flex flex-col gap-4" },
          h(ErrorText, { text: account.error }),
          account.status_error && !account.removed ? h("p", { className: "text-sm text-warning" }, account.status_error) : null,
          account.last_sync ? h("p", { className: "text-xs text-muted-foreground" }, "Last sync: " + new Date(account.last_sync).toLocaleString()) : null,
          h("div", { className: "flex flex-wrap gap-2" },
            !account.removed && editable ? toggle("account", "Account") : null,
            !account.removed ? toggle("notify", "Notifications") : null,
            !account.removed && settings.auth === "oauth" ? toggle("signin", "Sign in") : null,
            editable && (account.changed || account.removed)
              ? h(Button, { size: "sm", outlined: true, onClick: reset, disabled: busy }, account.removed ? "Restore" : "Reset to NixOS")
              : null,
            editable && !account.removed
              ? h(Button, { size: "sm", destructive: true, onClick: function () { props.onRemove(account); }, disabled: busy }, "Remove")
              : null),
          open === "account" ? h(AccountForm, { account: account, meta: props.meta, onSaved: done, onCancel: cancel }) : null,
          open === "notify" ? h(NotifyForm, { account: account, onSaved: done, onCancel: cancel }) : null,
          open === "signin" ? h(SignIn, { account: account, onSaved: done, onCancel: cancel }) : null)));
  }

  function TriageCard(props) {
    const form = useForm(props.triage);

    function save() {
      form.submit(function () { return post("/triage", form.values); },
        function () { props.onChanged("Saved the triage model."); });
    }

    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Triage model")),
      h(CardContent, null,
        h("div", { className: "flex flex-col gap-4" },
          h("p", { className: "text-xs text-muted-foreground" },
            "Empty values use auxiliary.hermes_mail_triage. Another provider or model needs " +
            "plugins.entries.hermes-mail.llm.allow_provider_override or allow_model_override."),
          h("div", { className: "grid grid-cols-1 gap-4 md:grid-cols-2" },
            h(TextField, { label: "Provider", value: form.values.provider, onChange: form.set("provider") }),
            h(TextField, { label: "Model", value: form.values.model, onChange: form.set("model") })),
          h(ErrorText, { text: form.error }),
          h("div", null, h(Button, { size: "sm", onClick: save, disabled: form.busy }, "Save triage model")))));
  }

  function MailPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [adding, setAdding] = useState(false);
    const [removing, setRemoving] = useState(null);
    const [busy, setBusy] = useState(false);
    const [generation, setGeneration] = useState(0);
    const { toast, showToast } = useToast();

    const refresh = useCallback(function () {
      return SDK.fetchJSON(API + "/settings").then(function (result) {
        if (result.ok) {
          setData(result);
          setGeneration(function (value) { return value + 1; });
        }
        setError(result.ok ? null : result.error);
      }, function (err) { setError(String(err)); });
    }, []);

    useEffect(function () { refresh(); }, [refresh]);

    function changed(message) {
      setAdding(false);
      showToast(message, "success");
      refresh();
    }

    function failed(message) {
      showToast(message, "error");
    }

    function remove() {
      setBusy(true);
      post(accountPath(removing.name) + "/remove").then(function () {
        changed("Removed the " + removing.name + " account.");
      }, function (err) { failed(err.message); }).then(function () {
        setBusy(false);
        setRemoving(null);
      });
    }

    const notice = error ? h(ErrorText, { text: error })
      : !data ? h("p", { className: "text-sm text-muted-foreground" }, "Loading…")
        : !data.editable ? h("p", { className: "text-xs text-muted-foreground" },
          "services.hermes-mail.webSettings is off, so the accounts come only from NixOS. Notifications stay editable.")
          : data.accounts.length === 0 ? h("p", { className: "text-sm text-muted-foreground" }, "No accounts yet.") : null;

    return h("div", { className: "flex flex-col gap-4" },
      h(Toast, { toast: toast }),
      h(ConfirmDialog, {
        open: removing !== null,
        title: removing ? "Remove the " + removing.name + " account?" : "",
        description: "The service deletes the index of its mail. The mail on the server stays. " +
          "A NixOS account stays in the list, and Restore brings it back.",
        confirmLabel: "Remove", destructive: true, loading: busy,
        onCancel: function () { setRemoving(null); }, onConfirm: remove,
      }),
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex flex-wrap items-start justify-between gap-2" },
            h("div", { className: "flex flex-col gap-1" },
              h(CardTitle, null, "Mail"),
              h("p", { className: "text-xs text-muted-foreground" }, data ? "hermes-maild socket: " + data.socket : "")),
            h("div", { className: "flex flex-wrap gap-2" },
              h(Button, { size: "sm", outlined: true, onClick: refresh }, "Refresh"),
              data && data.editable ? h(Button, { size: "sm", onClick: function () { setAdding(!adding); } }, "Add account") : null))),
        notice || (adding && data) ? h(CardContent, null,
          notice,
          adding && data ? h("div", { className: notice ? "mt-4" : "" }, h(AccountForm, {
            meta: data, onSaved: changed, onCancel: function () { setAdding(false); },
          })) : null) : null),
      data ? data.accounts.map(function (account) {
        return h(AccountCard, {
          key: account.name + ":" + generation, account: account, meta: data,
          onChanged: changed, onError: failed, onRemove: setRemoving,
        });
      }) : null,
      data ? h(TriageCard, { key: "triage:" + generation, triage: data.triage, onChanged: changed }) : null);
  }

  window.__HERMES_PLUGINS__.register("hermes-mail", MailPage);
})();
