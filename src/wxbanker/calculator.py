#    https://launchpad.net/wxbanker
#    calculator.py: Copyright 2007-2010 Mike Rooney <mrooney@ubuntu.com>
#
#    This file is part of wxBanker.
#
#    wxBanker is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    wxBanker is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with wxBanker.  If not, see <http://www.gnu.org/licenses/>.

import wx
from wx.lib.pubsub import Publisher #@UnresolvedImport
from wxbanker import localization #@UnusedImport


class SimpleCalculator(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent)
        self.numberClears = True

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.display = wx.TextCtrl(self, -1, '0.00',  style=wx.TE_RIGHT)
        self.display.Bind(wx.EVT_CHAR, self.onChar)
        sizer.Add(self.display, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 4)
        gs = wx.GridSizer(4, 4, 3, 3)

        for lbl in "C ()789/456*123-0.+=":
            if not lbl == ' ':
                btn = wx.Button(self, label=lbl)
                btn.Bind(wx.EVT_BUTTON, self.onButton)
            else:
                btn = wx.StaticText(self)
            gs.Add(btn, 1, wx.EXPAND)

        sizer.Add(gs, 1, wx.EXPAND)
        self.SetSizer(sizer)
        Publisher.subscribe(self.onPushChars, "CALCULATOR.PUSH_CHARS")
        wx.CallLater(50, self.display.SetInsertionPointEnd)

    def onPushChars(self, message):
        for char in message.data:
            self.onChar(char=char)
        self.Parent.Parent.SetExpanded(True)

    def onChar(self, event=None, char=None):
        """
        Handles characters, either from a button or a key typed into the text control.

        Supported: 0123456789./*-+
        Special:
            * 'c' or 'C': clears the text ctrl
            * '=' (or WXK_RETURN): evaluates the expression and displays the result
            * WXK_BACK: backspace, deletes the last character

        All other characters/keypresses are discarded.
        """
        if event:
            keycode = event.KeyCode
            if keycode == wx.WXK_BACK:
                self.display.Value = self.display.Value[:-1]
                self.display.SetInsertionPointEnd()
                self.numberClears = False
                return
            elif keycode == wx.WXK_RETURN:
                char = '='
            else:
                try:
                    char = chr(event.KeyCode)
                except ValueError:
                    # It may have been an arrow key press or something else not chr'able.
                    if event:
                        event.Skip()
                    return
        else:
            assert char

        if char == '=':
            self.onEnter()
        elif char.upper() == 'C':
            self.display.Value = ""
        elif char in '()0123456789./*-+':
            if char in '()0123456789.' and self.numberClears:
                self.display.Value = ""
            self.numberClears = False
            self.display.AppendText(char)
        else:
            # Not a valid character.
            return

        self.display.SetFocus()
        self.display.SetInsertionPointEnd()

    def onEnter(self, event=None):
        """
        Attempt to do a simple eval on the text.
        Note no implicit order of operations is respected.
        """
        try:
            # Multiply by float first, so 1/2 is 0.50 and not 0.00
            result = eval("1.*"+self.display.Value)
        except:
            return
        self.display.Value = "%.2f"%result
        self.numberClears = True
        self.display.SetInsertionPointEnd()

    def onButton(self, event=None):
        btn = event.EventObject
        command = btn.Label
        self.onChar(char=command)


class CollapsableWidget(wx.CollapsiblePane):
    def __init__(self, parent, widget, labels, *args, **kwargs):
        self.Labels = labels
        wx.CollapsiblePane.__init__(self, parent, label=self.Labels[0], style=wx.CP_DEFAULT_STYLE|wx.CP_NO_TLW_RESIZE)

        pane = self.GetPane()
        self.widget = widget(pane, *args, **kwargs)
        pane.Sizer = wx.BoxSizer()
        pane.Sizer.Add(self.widget, 1, wx.EXPAND)

        self.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, self.OnPaneChanged)

    def SetExpanded(self, expanded):
        """
        Set whether the contained widget is collapsed or expanded. If there will be no change, don't do anything.
        """
        if expanded != self.IsExpanded():
            self.Collapse(not expanded)
            self.OnPaneChanged()

    def OnPaneChanged(self, evt=None):
        """Redo the text and layout when the widget state is toggled."""
        self.Layout()
        
        # Change the labels
        expanded = int(self.IsExpanded())        
        self.Label = self.Labels[expanded]
        Publisher.sendMessage("CALCULATOR.TOGGLED", ("SHOW", "HIDE")[expanded])
