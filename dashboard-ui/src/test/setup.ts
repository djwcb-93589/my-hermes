import "@testing-library/jest-dom";

// jsdom 30 未实现 HTMLDialogElement 的 showModal/show/close 方法，
// 这里补最小 polyfill，仅维护 open 属性，供测试环境渲染 <dialog>。
if (typeof HTMLDialogElement !== "undefined") {
  const proto = HTMLDialogElement.prototype;
  if (typeof proto.showModal !== "function") {
    proto.showModal = function () {
      this.open = true;
    };
  }
  if (typeof proto.show !== "function") {
    proto.show = function () {
      this.open = true;
    };
  }
  if (typeof proto.close !== "function") {
    proto.close = function () {
      this.open = false;
    };
  }
}
