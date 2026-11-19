"""Small Windows desktop controller for the local MVP; rendering runs off the UI thread."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from .renderer import render


def start() -> None:
    app = tk.Tk()
    app.title("VKino Studio — локальный монтаж 0.1")
    app.geometry("720x430")
    app.minsize(620, 360)
    manifest = tk.StringVar()
    output = tk.StringVar()
    encoder = tk.StringVar(value="libx264")
    busy = tk.BooleanVar(value=False)
    messages: queue.Queue[tuple[str, str]] = queue.Queue()

    outer = ttk.Frame(app, padding=20)
    outer.pack(fill="both", expand=True)
    ttk.Label(outer, text="VKino Studio", font=("Segoe UI", 20, "bold")).pack(anchor="w")
    ttk.Label(outer, text="Локальный монтаж. Только утверждённые сцены; публикация отключена.").pack(anchor="w", pady=(2, 12))

    def choose_project() -> None:
        p = filedialog.askopenfilename(title="Открыть проект", filetypes=[("VKino JSON", "*.json")])
        if p:
            manifest.set(p)
            output.set(str(Path(p).parent / "output"))

    def choose_output() -> None:
        p = filedialog.askdirectory(title="Папка для видео")
        if p:
            output.set(p)

    for label, variable, choose in [("Файл проекта", manifest, choose_project),
                                    ("Папка результата", output, choose_output)]:
        line = ttk.Frame(outer)
        line.pack(fill="x", pady=5)
        ttk.Label(line, text=label, width=18).pack(side="left")
        ttk.Entry(line, textvariable=variable).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(line, text="Обзор…", command=choose).pack(side="left")

    codec_line = ttk.Frame(outer)
    codec_line.pack(fill="x", pady=8)
    ttk.Label(codec_line, text="Кодирование", width=18).pack(side="left")
    ttk.Combobox(codec_line, textvariable=encoder, width=18, state="readonly",
                 values=["libx264", "h264_nvenc"]).pack(side="left", padx=6)
    ttk.Label(codec_line, text="libx264 — надёжный вариант; NVENC требует совместимого драйвера").pack(side="left")
    status = tk.StringVar(value="Готово к запуску")
    ttk.Label(outer, textvariable=status).pack(anchor="w", pady=(8, 2))
    progress = ttk.Progressbar(outer, mode="indeterminate")
    progress.pack(fill="x", pady=6)

    def run_job() -> None:
        try:
            path = render(Path(manifest.get()), Path(output.get()), encoder.get(),
                          lambda msg: messages.put(("progress", msg)))
            messages.put(("success", str(path)))
        except Exception as exc:
            messages.put(("error", str(exc)))

    def launch() -> None:
        if busy.get():
            return
        if not manifest.get() or not output.get():
            messagebox.showinfo("Нужен проект", "Выберите JSON-файл проекта и папку результата")
            return
        busy.set(True)
        button.config(state="disabled")
        progress.start(10)
        threading.Thread(target=run_job, daemon=True).start()

    button = ttk.Button(outer, text="Собрать предпросмотр MP4", command=launch)
    button.pack(anchor="w", pady=12)

    def update() -> None:
        try:
            while True:
                kind, text = messages.get_nowait()
                status.set(text)
                if kind in {"success", "error"}:
                    busy.set(False)
                    button.config(state="normal")
                    progress.stop()
                    if kind == "error":
                        messagebox.showerror("Ошибка монтажа", text)
                    else:
                        messagebox.showinfo("Видео готово", "Черновик сохранён:\n" + text)
        except queue.Empty:
            pass
        app.after(120, update)

    app.after(120, update)
    app.mainloop()
