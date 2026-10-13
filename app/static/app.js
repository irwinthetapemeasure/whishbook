// Ask before destructive actions (forms marked with data-confirm).
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Keep the field being typed in visible above the on-screen keyboard. Android browsers resize the
// page for the keyboard (see the viewport meta); iOS Safari only shrinks the visual viewport, so
// check whether the focused field is still inside it and scroll it back into view if not.
(function () {
  var vv = window.visualViewport;
  if (!vv) return;
  function field() {
    var el = document.activeElement;
    return el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) && el.type !== "hidden" ? el : null;
  }
  function reveal() {
    var el = field();
    if (!el) return;
    var r = el.getBoundingClientRect();
    var top = vv.offsetTop, bottom = vv.offsetTop + vv.height;
    if (r.top < top + 8 || r.bottom > bottom - 16) {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
  var timer;
  function later() { clearTimeout(timer); timer = setTimeout(reveal, 120); }
  vv.addEventListener("resize", later);
  document.addEventListener("focusin", function () { setTimeout(reveal, 350); });
})();
