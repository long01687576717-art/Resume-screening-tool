"""API Key 配置窗口：输入 DeepSeek Key、测试连接、选择模型，保存到项目根目录的 .env 文件。

用法：python setup_key.py
"""
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from openai import OpenAI, OpenAIError

from llm.client import BASE_URL, ROOT, read_env_file, save_env

ENV_PATH = ROOT / ".env"

DEFAULT_MODEL = "deepseek-v4-pro"


class KeyWindow:
    def __init__(self, root):
        self.root = root
        root.title("DeepSeek API 配置")
        root.resizable(False, False)
        saved = read_env_file()

        frame = ttk.Frame(root, padding=16)
        frame.grid()

        ttk.Label(frame, text="API Key：").grid(row=0, column=0, sticky="w", pady=4)
        self.key_var = tk.StringVar(value=saved.get("DEEPSEEK_API_KEY", ""))
        self.key_entry = ttk.Entry(frame, textvariable=self.key_var, width=48, show="•")
        self.key_entry.grid(row=0, column=1, pady=4)
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="显示", variable=self.show_var, command=self._toggle_show).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(frame, text="模型：").grid(row=1, column=0, sticky="w", pady=4)
        self.model_var = tk.StringVar(value=saved.get("DEEPSEEK_MODEL", DEFAULT_MODEL))
        self.model_box = ttk.Combobox(frame, textvariable=self.model_var, width=46)
        self.model_box.grid(row=1, column=1, pady=4)

        self.status_var = tk.StringVar(value="填写 Key 后，建议先点「测试连接」获取可用模型")
        ttk.Label(frame, textvariable=self.status_var, foreground="#555", wraplength=460).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(8, 12))

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=3, sticky="e")
        self.test_button = ttk.Button(buttons, text="测试连接", command=self._test)
        self.test_button.grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="保存", command=self._save).grid(row=0, column=1, padx=4)

        self.key_entry.focus_set()

    def _toggle_show(self):
        self.key_entry.config(show="" if self.show_var.get() else "•")

    def _test(self):
        key = self.key_var.get().strip()
        if not key:
            messagebox.showwarning("提示", "请先填写 API Key")
            return
        self.test_button.config(state="disabled")
        self.status_var.set("正在连接 DeepSeek……")
        threading.Thread(target=self._fetch_models, args=(key,), daemon=True).start()

    def _fetch_models(self, key):
        try:
            models = sorted(m.id for m in OpenAI(api_key=key, base_url=BASE_URL, timeout=20).models.list())
            self.root.after(0, self._on_models, models, None)
        except OpenAIError as e:
            self.root.after(0, self._on_models, [], e)

    def _on_models(self, models, error):
        self.test_button.config(state="normal")
        if error:
            self.status_var.set(f"连接失败：{error}")
            return
        self.model_box.config(values=models)
        if self.model_var.get() in models:
            self.status_var.set(f"连接成功。可用模型：{'、'.join(models)}")
        else:
            self.status_var.set(
                f"连接成功，但当前填写的「{self.model_var.get()}」不在可用列表中，请从下拉框选择。"
                f"可用模型：{'、'.join(models)}")

    def _save(self):
        key, model = self.key_var.get().strip(), self.model_var.get().strip()
        if not key or not model:
            messagebox.showwarning("提示", "API Key 和模型都需要填写")
            return
        save_env({"DEEPSEEK_API_KEY": key, "DEEPSEEK_MODEL": model})
        messagebox.showinfo("已保存", f"已保存到 {ENV_PATH}")
        self.root.destroy()


if __name__ == "__main__":
    window = tk.Tk()
    KeyWindow(window)
    window.mainloop()
