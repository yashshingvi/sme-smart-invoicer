(function () {
  "use strict";

  // ===================== Tab navigation =====================
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const target = btn.dataset.tab;
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      document.getElementById("tab-" + target).classList.add("active");
      if (target === "dashboard") loadDashboard();
      else if (target === "inbox") loadInbox();
      else if (target === "vendors") loadVendors();
    });
  });

  // ===================== Toast =====================
  function toast(msg, cls) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className = "toast " + (cls || "");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.add("hidden"), 3000);
  }

  // ===================== Dashboard =====================
  async function loadDashboard() {
    try {
      const r = await fetch("/api/dashboard/stats");
      const s = await r.json();
      document.getElementById("stat-total").textContent = s.total_invoices;
      document.getElementById("stat-payable").textContent = "₹" + fmt(s.total_payable);
      document.getElementById("stat-overdue").textContent = s.overdue_count;
      document.getElementById("stat-overdue-amount").textContent = "₹" + fmt(s.overdue_amount);
      document.getElementById("stat-gst").textContent = "₹" + fmt(s.gst_input_credit);
      document.getElementById("stat-review").textContent = s.needs_review;

      // Top vendors
      const tv = document.getElementById("top-vendors").querySelector("tbody");
      tv.innerHTML = "";
      (s.top_vendors || []).forEach((v) => {
        const tr = document.createElement("tr");
        tr.innerHTML = "<td>" + esc(v.name) + "</td><td>₹" + fmt(v.total) + "</td>";
        tv.appendChild(tr);
      });

      // Monthly chart
      const chart = document.getElementById("monthly-chart");
      chart.innerHTML = "";
      const maxVal = Math.max(1, ...(s.monthly_spend || []).map((m) => m.total));
      (s.monthly_spend || []).forEach((m) => {
        const col = document.createElement("div");
        col.className = "bar-col";
        const h = Math.round((m.total / maxVal) * 140);
        col.innerHTML =
          '<div class="bar-value">₹' +
          fmt(m.total) +
          '</div><div class="bar" style="height:' +
          h +
          'px"></div><div class="bar-label">' +
          esc(m.month) +
          "</div>";
        chart.appendChild(col);
      });

      // Recent invoices (last 10 via inbox endpoint)
      const ri = await fetch("/api/invoices?limit=10");
      const rd = await ri.json();
      const rt = document.getElementById("recent-invoices").querySelector("tbody");
      rt.innerHTML = "";
      rd.forEach((inv) => renderInboxRow(rt, inv));
    } catch (e) {
      console.error("Dashboard load error", e);
    }
  }

  function fmt(n) {
    return Number(n || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  // ===================== Inbox =====================
  async function loadInbox() {
    const search = document.getElementById("search").value.trim();
    const status = document.getElementById("filter-status").value;
    const payment = document.getElementById("filter-payment").value;
    const params = new URLSearchParams();
    if (search) params.set("q", search);
    if (status) params.set("status", status);
    if (payment) params.set("payment_status", payment);
    params.set("limit", "200");
    try {
      const r = await fetch("/api/invoices?" + params.toString());
      const data = await r.json();
      const tbody = document.getElementById("inbox-table").querySelector("tbody");
      tbody.innerHTML = "";
      data.forEach((inv) => renderInboxRow(tbody, inv));
    } catch (e) {
      console.error("Inbox load error", e);
    }
  }

  function renderInboxRow(tbody, inv) {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => openReview(inv.id));
    tr.innerHTML =
      "<td>" +
      esc(inv.invoice_number || "—") +
      "</td>" +
      "<td>" +
      esc(inv.vendor ? inv.vendor.name : "—") +
      "</td>" +
      "<td>" +
      (inv.invoice_date || "—") +
      "</td>" +
      "<td>" +
      (inv.due_date || "—") +
      "</td>" +
      "<td>₹" +
      fmt(inv.total) +
      "</td>" +
      '<td><span class="badge ' +
      esc(inv.status) +
      '">' +
      esc(inv.status) +
      "</span></td>" +
      '<td><span class="badge ' +
      esc(inv.payment_status) +
      '">' +
      esc(inv.payment_status) +
      "</span></td>" +
      '<td><button class="badge" onclick="event.stopPropagation();openReview(\'' +
      inv.id +
      "')\">Review</button></td>";
    tbody.appendChild(tr);
  }

  // Inbox search/filter bindings
  document.getElementById("search").addEventListener("input", debounce(loadInbox, 300));
  document.getElementById("filter-status").addEventListener("change", loadInbox);
  document.getElementById("filter-payment").addEventListener("change", loadInbox);
  document.getElementById("refresh-inbox").addEventListener("click", loadInbox);

  function debounce(fn, ms) {
    let t;
    return function () {
      clearTimeout(t);
      t = setTimeout(fn, ms);
    };
  }

  // ===================== Vendors =====================
  async function loadVendors() {
    try {
      const r = await fetch("/api/vendors");
      const data = await r.json();
      const tbody = document.getElementById("vendors-table").querySelector("tbody");
      tbody.innerHTML = "";
      data.forEach((v) => {
        const tr = document.createElement("tr");
        tr.innerHTML =
          "<td>" +
          esc(v.name) +
          "</td>" +
          "<td>" +
          (v.gstin || "—") +
          "</td>" +
          "<td>" +
          (v.state_code || "—") +
          "</td>" +
          "<td>" +
          (v.payment_terms_days || 30) +
          " days</td>";
        tbody.appendChild(tr);
      });
    } catch (e) {
      console.error("Vendors load error", e);
    }
  }

  // ===================== Upload =====================
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("drag");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag");
    handleFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener("change", () => handleFiles(fileInput.files));

  async function handleFiles(files) {
    const log = document.getElementById("upload-log");
    for (const file of files) {
      const item = document.createElement("div");
      item.className = "log-item";
      item.textContent = "Uploading " + file.name + "...";
      log.prepend(item);
      try {
        const form = new FormData();
        form.append("file", file);
        const r = await fetch("/api/invoices/upload", { method: "POST", body: form });
        const data = await r.json();
        if (r.ok) {
          item.className = "log-item success";
          item.innerHTML =
            '<span>✅ ' +
            esc(file.name) +
            " — " +
            esc(data.status) +
            "</span>" +
            '<button onclick="openReview(\'' +
            data.id +
            "')\" style=\"cursor:pointer;padding:4px 8px;border-radius:4px;border:1px solid var(--border)\">Review</button>";
        } else {
          item.className = "log-item error";
          const detail = data.detail || "Upload failed";
          item.innerHTML =
            "<span>❌ " +
            esc(file.name) +
            " — " +
            esc(typeof detail === "object" ? detail.message : detail) +
            "</span>";
        }
      } catch (e) {
        item.className = "log-item error";
        item.textContent = "❌ " + file.name + " — Network error";
      }
    }
  }

  // ===================== Review modal =====================
  let currentInvoiceId = null;

  async function openReview(id) {
    currentInvoiceId = id;
    document.getElementById("review-modal").classList.remove("hidden");
    try {
      const r = await fetch("/api/invoices/" + id);
      const inv = await r.json();
      document.getElementById("review-title").textContent =
        "Invoice: " + (inv.invoice_number || "New");

      // Populate fields
      document.getElementById("f-vendor-name").value = inv.vendor ? inv.vendor.name : "";
      document.getElementById("f-vendor-gstin").value = inv.vendor ? (inv.vendor.gstin || "") : "";
      document.getElementById("f-invoice-number").value = inv.invoice_number || "";
      document.getElementById("f-invoice-date").value = inv.invoice_date || "";
      document.getElementById("f-due-date").value = inv.due_date || "";
      document.getElementById("f-subtotal").value = inv.subtotal || 0;
      document.getElementById("f-cgst").value = inv.cgst || 0;
      document.getElementById("f-sgst").value = inv.sgst || 0;
      document.getElementById("f-igst").value = inv.igst || 0;
      document.getElementById("f-total").value = inv.total || 0;
      document.getElementById("f-status").value = inv.status || "PENDING";
      document.getElementById("f-payment-status").value = inv.payment_status || "UNPAID";
      document.getElementById("f-paid-amount").value = inv.paid_amount || 0;
      document.getElementById("f-notes").value = inv.review_notes || "";

      // Preview
      const preview = document.getElementById("review-preview");
      if (inv.file_path) {
        const ext = inv.file_path.split(".").pop().toLowerCase();
        if (ext === "pdf") {
          preview.innerHTML =
            '<embed src="/api/invoices/' + id + '/file" type="application/pdf" />';
        } else {
          preview.innerHTML =
            '<img src="/api/invoices/' + id + '/file" alt="invoice preview" />';
        }
      } else {
        preview.textContent = "No file attached";
      }

      // Raw OCR
      document.getElementById("review-ocr").textContent = inv.raw_ocr_text || "(none)";

      // Line items
      const lit = document.getElementById("lineitems-table").querySelector("tbody");
      lit.innerHTML = "";
      (inv.line_items || []).forEach((li) => {
        const tr = document.createElement("tr");
        tr.innerHTML =
          '<td><input value="' +
          esc(li.description || "") +
          '" data-field="description" /></td>' +
          '<td><input value="' +
          esc(li.hsn_code || "") +
          '" data-field="hsn_code" size="6" /></td>' +
          '<td><input value="' +
          (li.quantity || 1) +
          '" data-field="quantity" type="number" step="0.01" size="6" /></td>' +
          '<td><input value="' +
          (li.rate || 0) +
          '" data-field="rate" type="number" step="0.01" size="8" /></td>' +
          '<td><input value="' +
          (li.gst_rate || 18) +
          '" data-field="gst_rate" type="number" step="0.01" size="6" /></td>' +
          '<td><input value="' +
          (li.total || 0) +
          '" data-field="total" type="number" step="0.01" size="10" /></td>' +
          '<td><button onclick="this.closest(\'tr\').remove()" style="border:0;background:transparent;cursor:pointer;color:var(--danger)">×</button></td>';
        lit.appendChild(tr);
      });
    } catch (e) {
      console.error("Review load error", e);
      toast("Failed to load invoice", "error");
    }
  }

  document.getElementById("review-close").addEventListener("click", () => {
    document.getElementById("review-modal").classList.add("hidden");
    currentInvoiceId = null;
  });

  document.getElementById("add-line").addEventListener("click", () => {
    const tbody = document.getElementById("lineitems-table").querySelector("tbody");
    const tr = document.createElement("tr");
    tr.innerHTML =
      '<td><input value="" data-field="description" /></td>' +
      '<td><input value="" data-field="hsn_code" size="6" /></td>' +
      '<td><input value="1" data-field="quantity" type="number" step="0.01" size="6" /></td>' +
      '<td><input value="0" data-field="rate" type="number" step="0.01" size="8" /></td>' +
      '<td><input value="18" data-field="gst_rate" type="number" step="0.01" size="6" /></td>' +
      '<td><input value="0" data-field="total" type="number" step="0.01" size="10" /></td>' +
      '<td><button onclick="this.closest(\'tr\').remove()" style="border:0;background:transparent;cursor:pointer;color:var(--danger)">×</button></td>';
    tbody.appendChild(tr);
  });

  function collectLineItems() {
    const rows = document.querySelectorAll("#lineitems-table tbody tr");
    const items = [];
    rows.forEach((tr) => {
      const get = (field) => {
        const inp = tr.querySelector('[data-field="' + field + '"]');
        return inp ? inp.value : "";
      };
      items.push({
        description: get("description"),
        hsn_code: get("hsn_code") || null,
        quantity: parseFloat(get("quantity")) || 1,
        rate: parseFloat(get("rate")) || 0,
        gst_rate: parseFloat(get("gst_rate")) || 18,
        total: parseFloat(get("total")) || 0,
      });
    });
    return items;
  }

  async function saveInvoice(approve) {
    if (!currentInvoiceId) return;
    const payload = {
      vendor_name: document.getElementById("f-vendor-name").value || null,
      vendor_gstin: document.getElementById("f-vendor-gstin").value || null,
      invoice_number: document.getElementById("f-invoice-number").value || null,
      invoice_date: document.getElementById("f-invoice-date").value || null,
      due_date: document.getElementById("f-due-date").value || null,
      subtotal: parseFloat(document.getElementById("f-subtotal").value) || 0,
      cgst: parseFloat(document.getElementById("f-cgst").value) || 0,
      sgst: parseFloat(document.getElementById("f-sgst").value) || 0,
      igst: parseFloat(document.getElementById("f-igst").value) || 0,
      total: parseFloat(document.getElementById("f-total").value) || 0,
      status: approve ? "APPROVED" : document.getElementById("f-status").value,
      payment_status: document.getElementById("f-payment-status").value,
      paid_amount: parseFloat(document.getElementById("f-paid-amount").value) || 0,
      review_notes: document.getElementById("f-notes").value || null,
      line_items: collectLineItems(),
    };
    try {
      const r = await fetch("/api/invoices/" + currentInvoiceId, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        toast("Invoice saved ✓", "success");
        openReview(currentInvoiceId);
      } else {
        const e = await r.json();
        toast("Save failed: " + (e.detail || "Server error"), "error");
      }
    } catch (e) {
      toast("Network error", "error");
    }
  }

  async function deleteInvoice() {
    if (!currentInvoiceId) return;
    if (!confirm("Permanently delete this invoice?")) return;
    try {
      const r = await fetch("/api/invoices/" + currentInvoiceId, { method: "DELETE" });
      if (r.ok) {
        document.getElementById("review-modal").classList.add("hidden");
        currentInvoiceId = null;
        toast("Invoice deleted", "success");
        loadInbox();
      } else {
        toast("Delete failed", "error");
      }
    } catch (e) {
      toast("Network error", "error");
    }
  }

  document.getElementById("save-invoice").addEventListener("click", () => saveInvoice(false));
  document.getElementById("approve-invoice").addEventListener("click", () => saveInvoice(true));
  document.getElementById("delete-invoice").addEventListener("click", deleteInvoice);

  // ===================== Initial load =====================
  loadDashboard();
})();
