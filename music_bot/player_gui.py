import wx
import os
from player_core import MusicPlayer

class MusicPlayerFrame(wx.Frame):
    def __init__(self):
        super().__init__(parent=None, title='Personal YouTube Music Player', size=(600, 450))
        self.panel = wx.Panel(self)
        self.player = MusicPlayer()
        self.current_results = []
        
        # Callbacks
        self.player.on_track_loaded = self.on_track_loaded
        self.player.on_track_finished = self.on_track_finished
        self.player.on_search_results = self.on_search_results_received
        
        self.setup_ui()
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Show()

    def setup_ui(self):
        vbox = wx.BoxSizer(wx.VERTICAL)
        
        # Top: Search Bar
        hbox1 = wx.BoxSizer(wx.HORIZONTAL)
        lbl_search = wx.StaticText(self.panel, label="Search YouTube:")
        self.txt_search = wx.TextCtrl(self.panel, style=wx.TE_PROCESS_ENTER)
        self.txt_search.SetName("Search Box")
        self.txt_search.Bind(wx.EVT_TEXT_ENTER, self.on_search)
        
        self.btn_search = wx.Button(self.panel, label="Search")
        self.btn_search.SetName("Search Button")
        self.btn_search.Bind(wx.EVT_BUTTON, self.on_search)
        
        hbox1.Add(lbl_search, flag=wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, border=8)
        hbox1.Add(self.txt_search, proportion=1, flag=wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, border=10)
        hbox1.Add(self.btn_search, flag=wx.ALIGN_CENTER_VERTICAL)
        vbox.Add(hbox1, flag=wx.EXPAND | wx.ALL, border=15)
        
        # Middle: Search Results
        lbl_results = wx.StaticText(self.panel, label="Search Results (Select and press Enter to play):")
        self.list_results = wx.ListBox(self.panel, style=wx.LB_SINGLE)
        self.list_results.SetName("Search Results List")
        self.list_results.Bind(wx.EVT_LISTBOX_DCLICK, self.on_play_selected)
        self.list_results.Bind(wx.EVT_KEY_DOWN, self.on_listbox_key)
        
        vbox.Add(lbl_results, flag=wx.LEFT | wx.RIGHT, border=15)
        vbox.Add(self.list_results, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)
        
        # Status Track Info
        self.lbl_status = wx.StaticText(self.panel, label="Ready", style=wx.ALIGN_CENTER)
        self.lbl_status.SetName("Status Text")
        font = self.lbl_status.GetFont()
        font.MakeBold()
        self.lbl_status.SetFont(font)
        vbox.Add(self.lbl_status, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=15)
        
        # Bottom: Controls
        hbox2 = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_pause = wx.Button(self.panel, label="Pause")
        self.btn_pause.SetName("Pause Button")
        self.btn_pause.Bind(wx.EVT_BUTTON, self.on_pause)
        self.btn_pause.Disable()
        
        self.btn_stop = wx.Button(self.panel, label="Stop")
        self.btn_stop.SetName("Stop Button")
        self.btn_stop.Bind(wx.EVT_BUTTON, self.on_stop)
        self.btn_stop.Disable()
        
        lbl_vol = wx.StaticText(self.panel, label="Volume:")
        self.slider_vol = wx.Slider(self.panel, value=50, minValue=0, maxValue=100, style=wx.SL_HORIZONTAL)
        self.slider_vol.SetName("Volume Slider")
        self.slider_vol.Bind(wx.EVT_SLIDER, self.on_volume)
        
        hbox2.Add(self.btn_pause, flag=wx.RIGHT, border=10)
        hbox2.Add(self.btn_stop, flag=wx.RIGHT, border=20)
        hbox2.Add(lbl_vol, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        hbox2.Add(self.slider_vol, proportion=1, flag=wx.ALIGN_CENTER_VERTICAL)
        
        vbox.Add(hbox2, flag=wx.EXPAND | wx.ALL, border=15)
        
        self.panel.SetSizer(vbox)
        self.Centre()
        
        self.txt_search.SetFocus()

    def on_search(self, event):
        query = self.txt_search.GetValue().strip()
        if not query: return
            
        self.lbl_status.SetLabel("Searching...")
        self.btn_search.Disable()
        self.txt_search.Disable()
        self.list_results.Clear()
        self.current_results = []
        
        self.player.search(query)

    def on_search_results_received(self, results):
        def update_ui():
            self.btn_search.Enable()
            self.txt_search.Enable()
            self.current_results = results
            
            self.list_results.Clear()
            if not results:
                self.lbl_status.SetLabel("No results found.")
                self.txt_search.SetFocus()
                return
                
            for res in results:
                self.list_results.Append(res['title'])
                
            self.lbl_status.SetLabel(f"Found {len(results)} results.")
            self.list_results.SetFocus()
            if self.list_results.GetCount() > 0:
                self.list_results.SetSelection(0)
                
        wx.CallAfter(update_ui)

    def on_listbox_key(self, event):
        if event.GetKeyCode() == wx.WXK_RETURN:
            self.on_play_selected(None)
        else:
            event.Skip()

    def on_play_selected(self, event):
        sel = self.list_results.GetSelection()
        if sel != wx.NOT_FOUND and sel < len(self.current_results):
            track = self.current_results[sel]
            self.lbl_status.SetLabel(f"Loading: {track['title']}...")
            self.btn_pause.Disable()
            self.btn_stop.Disable()
            self.player.play(track['webpage_url'], track['title'])

    def on_track_loaded(self, success, title):
        def update_ui():
            if success:
                self.lbl_status.SetLabel(f"Playing: {title}")
                self.btn_pause.Enable()
                self.btn_pause.SetLabel("Pause")
                self.btn_stop.Enable()
            else:
                self.lbl_status.SetLabel(title)
        wx.CallAfter(update_ui)

    def on_track_finished(self):
        def update_ui():
            self.lbl_status.SetLabel("Playback finished.")
            self.btn_pause.Disable()
            self.btn_stop.Disable()
            self.btn_pause.SetLabel("Pause")
        wx.CallAfter(update_ui)

    def on_pause(self, event):
        is_paused = self.player.toggle_pause()
        self.btn_pause.SetLabel("Resume" if is_paused else "Pause")
        if is_paused:
            self.lbl_status.SetLabel(f"[Paused] {self.lbl_status.GetLabel().replace('[Paused] ', '')}")
        else:
            self.lbl_status.SetLabel(self.lbl_status.GetLabel().replace('[Paused] ', ''))

    def on_stop(self, event):
        self.player.stop()
        self.on_track_finished()

    def on_volume(self, event):
        vol = self.slider_vol.GetValue()
        self.player.set_volume(vol)

    def on_close(self, event):
        self.player.cleanup()
        self.Destroy()

if __name__ == '__main__':
    app = wx.App()
    frame = MusicPlayerFrame()
    app.MainLoop()
