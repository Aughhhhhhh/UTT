import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import recipe


class App:
    def __init__(self, root):
        self.root = root
        self._queue = queue.Queue()
        self._running = False
        self._output_folder = None

        root.title("Skate 3 - Current Character Items")
        root.geometry("680x520")
        root.minsize(480, 360)

        top = ttk.Frame(root, padding=8)
        top.pack(fill=tk.X)

        self.scan_button = ttk.Button(top, text="Scan Character", command=self.start_scan)
        self.scan_button.pack(side=tk.LEFT)

        self.open_button = ttk.Button(top, text="Open Output Folder", command=self.open_output, state=tk.DISABLED)
        self.open_button.pack(side=tk.LEFT, padx=(8, 0))

        self.text = scrolledtext.ScrolledText(root, wrap=tk.WORD, state=tk.DISABLED, font=("Consolas", 10))
        self.text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.status = ttk.Label(root, text="Ready", anchor=tk.W, padding=(8, 0))
        self.status.pack(fill=tk.X)

        self.root.after(100, self.poll_queue)

    def log(self, message):
        self.text.config(state=tk.NORMAL)
        self.text.insert(tk.END, message + "\n")
        self.text.see(tk.END)
        self.text.config(state=tk.DISABLED)
        self.status.config(text=message)

    def start_scan(self):
        if self._running:
            return
        self._running = True
        self.scan_button.config(state=tk.DISABLED)
        self.log("Scanning for RPCS3...")
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def scan_worker(self):
        try:
            result = recipe.scan_and_save()
        except Exception as e:
            self._queue.put(("error", str(e)))
        else:
            self._queue.put(("done", result))

    def poll_queue(self):
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            if not self._running:
                self.status.config(text="Ready")
            self.root.after(100, self.poll_queue)
            return
        except tk.TclError:
            return

        if kind == "error":
            self.log(f"Error: {payload}")
            messagebox.showerror("Scan Failed", payload)
        else:
            items = payload["items"]
            self.log(f"Found {len(items)} items")
            self.show_items(items)
            self.log(f"Saved to: {payload['txt_path']}")
            self._output_folder = payload["output_folder"]
            self.open_button.config(state=tk.NORMAL)

        self._running = False
        self.scan_button.config(state=tk.NORMAL)
        self.root.after(100, self.poll_queue)

    def show_items(self, items):
        self.text.config(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, recipe.format_items(items))
        self.text.config(state=tk.DISABLED)

    def open_output(self):
        if self._output_folder is not None and os.path.isdir(self._output_folder):
            os.startfile(str(self._output_folder))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
