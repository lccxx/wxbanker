#!/usr/bin/env python
#
#    https://launchpad.net/wxbanker
#    wxbanker.py: Copyright 2007-2010 Mike Rooney <mrooney@ubuntu.com>
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

import os #@UnusedImport

# wxPython
try:
    import wxversion
except ImportError:
    # Maybe we don't have wxversion but still have wx >= 2.8?
    import wx, wx.aui
else:
    # Okay, let's try to get >= 2.8
    wxversion.ensureMinimal("2.8")
    import wx, wx.aui #@UnusedImport

from wx.lib.pubsub import Publisher #@UnresolvedImport

# wxBanker
from wxbanker.menubar import BankMenuBar
from wxbanker.brandedframe import BrandedFrame
from wxbanker import localization, messagepanel, debug #@UnusedImport
from wxbanker import managetab


class BankerPanel(wx.Panel):
    def __init__(self, parent, bankController):
        wx.Panel.__init__(self, parent)
        self.bankController = bankController
        
        self.mainPanel = managetab.MainPanel(self, bankController)
        
        Publisher.subscribe(self.onRecurringTransactionAdded, "recurringtransaction.created")
        Publisher.subscribe(self.onRecurringTransactionUpdated, "recurringtransaction.updated")

        self.Sizer = wx.BoxSizer(wx.VERTICAL)
        self.Sizer.Add(self.mainPanel, 1, wx.EXPAND)
        
        wx.CallLater(1000, self.CheckRecurringTransactions)
        
    def AddMessagePanel(self, panel):
        self.Sizer.Insert(0, panel, 0, wx.EXPAND)
        self.Layout()

    def onRecurringTransactionUpdated(self, message):
        # If a recurring transaction was updated, perhaps there are more to enter.
        self.CheckRecurringTransactions()
        
    def CheckRecurringTransactions(self):
        recurrings = self.bankController.Model.GetRecurringTransactions()
        # Figure out how many due recurring transactions there are.
        untransacted = []
        totalTransactions = 0
        for recurring in recurrings:
            dates = recurring.GetUntransactedDates()
            if dates:
                untransacted.append((recurring, dates))
                totalTransactions += len(dates)
                
        # If there aren't any untransacted transactions, we are done.
        if not untransacted:
            return 0
        
        # Generate an appropriate message.
        lines = []
        if len(untransacted) == 1:
            recurring, dates = untransacted[0]
            message = _('The recurring transaction "%(description)s" has %(num)i transactions ready for %(amount)s on %(datelist)s.')
            amount = recurring.RenderAmount()
            datelist = ", ".join([str(d) for d in dates])
            message = message % {'description': recurring.Description, 'amount': amount, 'num': len(dates), 'datelist': datelist}
        else:
            message = _('%(num)i recurring transactions have a total of %(totalnum)i transactions ready.')
            message = message % {'num': len(untransacted), 'totalnum': totalTransactions}
            for recurring, dates in untransacted:
                lines.append(recurring.GetDueString())
            
        # Create the message panel.
        mpanel = messagepanel.MessagePanel(self, message)
        
        # If there are lines to display, add the button and callback
        if lines:
            mpanel.AddLines(lines)
            mpanel.PushButton(_("Preview"), mpanel.ToggleLines)
            
        # Create the callback to perform the transactions.
        def performer(event=None):
            for recurring, dates in untransacted:
                recurring.PerformTransactions()
            mpanel.Dismiss()
        
        # Add a button which will enter the transactions on a click.
        mpanel.PushButton(_("Perform"), performer)
        
        # Show the message.
        self.AddMessagePanel(mpanel)
        
        return len(recurrings)
    
    def onRecurringTransactionAdded(self, message):
        account, recurring = message.data #@UnusedVariable
        dates = recurring.GetUntransactedDates()
        
        # If there are transactions due, inform the user via the normal route.
        if dates:
            self.CheckRecurringTransactions()
        # Otherwise, just inform the user it was added, and the first date.
        else:
            message = _("Recurring transaction successfully added.")
            firstDate = recurring.GetNext()
            message += " " + _("The first transaction will occur on %(date)s") % {"date": firstDate}
            mpanel = messagepanel.MessagePanel(self, message)
            self.AddMessagePanel(mpanel)
        
        
class BankerFrame(BrandedFrame):
    def __init__(self, bankController, welcome):
        # Load our window settings.
        config = wx.Config.Get()
        size = config.ReadInt('SIZE_X'), config.ReadInt('SIZE_Y')
        pos = config.ReadInt('POS_X'), config.ReadInt('POS_Y')

        BrandedFrame.__init__(self, None, title="wxBanker", size=size, pos=pos)

        self.Panel = BankerPanel(self, bankController)

        Publisher.subscribe(self.onQuit, "quit")
        Publisher.subscribe(self.onWarning, "warning")
        
        if welcome:
            Publisher.subscribe(self.onFirstRun, "first run")

        self.Bind(wx.EVT_SIZE, self.OnSize)
        self.Bind(wx.EVT_MOVE, self.OnMove)
        self.Bind(wx.EVT_CLOSE, self.OnClose)

        menuBar = BankMenuBar(bankController)
        self.SetMenuBar(menuBar)
        #self.CreateStatusBar()

        self.Bind(wx.EVT_MENU, menuBar.onMenuEvent)

    def OnMove(self, event):
        config = wx.Config.Get()

        x, y = self.GetPosition()
        config.WriteInt("POS_X", x)
        config.WriteInt("POS_Y", y)

        event.Skip()

    def OnSize(self, event):
        config = wx.Config.Get()

        if not self.IsMaximized():
            x, y = self.GetSize()
            config.WriteInt('SIZE_X', x)
            config.WriteInt('SIZE_Y', y)

        config.WriteBool('IsMaximized', self.IsMaximized())
        event.Skip()

    def OnClose(self, event):
        """This gets called on Alt+F4."""
        event.Skip() # This must be first, so handlers can override it.
        Publisher.sendMessage("exiting", event)
        
    def onQuit(self, message):
        """The subscriber to the pubsub event which anyone can trigger."""
        # This will trigger the event, triggering OnClose above.
        self.Close()

    def onFirstRun(self, message):
        welcomeMsg = _("It looks like this is your first time using wxBanker!")
        welcomeMsg += "\n\n" + _("To get started, add an account using the account control in the top left corner.")
        welcomeMsg += " " + _("The buttons in the account control allow you to add, rename, configure, and remove an account, respectively.")
        welcomeMsg += "\n\n" + _("Once you have created an account you can add transactions to it (such as your initial balance) using the controls below the grid on the bottom right.")
        welcomeMsg += "\n\n" + _("Have fun!")
        #wx.TipWindow(self, welcomeMsg, maxLength=300)
        dlg = wx.MessageDialog(self, welcomeMsg, _("Welcome!"), style=wx.OK|wx.ICON_INFORMATION)
        dlg.ShowModal()
        dlg.Destroy()

    def onWarning(self, message):
        warning = message.topic[1]
        if warning == "dirty exit":
            event = message.data
            title = _("Save changes?")
            msg = _("You have made changes since the last save. Would you like to save before exiting?")
            msg += "\n\n" + _("Note that enabling auto-save from the File menu will eliminate the need for manual saving.")
            dlg = wx.MessageDialog(self, msg, title, style=wx.CANCEL|wx.YES_NO|wx.ICON_WARNING)
            result = dlg.ShowModal()
            if result == wx.ID_YES:
                Publisher.sendMessage("user.saved")
            elif result == wx.ID_CANCEL:
                # The user cancelled the close, so cancel the event skip.
                event.Skip(False)
            dlg.Destroy()


def init(path=None, welcome=True):
    from wxbanker import fileservice #@UnusedImport
    from wxbanker.controller import Controller

    bankController = Controller(path)
    
    # We can initialize the wx locale now that the wx.App is initialized.
    localization.initWxLocale()
    
    # Push our custom art provider.
    import wx.lib.art.img2pyartprov as img2pyartprov
    from wxbanker.art import silk, transparent
    for provider in (silk, transparent):
        wx.ArtProvider.Push(img2pyartprov.Img2PyArtProvider(provider))

    # Initialize the wxBanker frame!
    frame = BankerFrame(bankController, welcome) #@UnusedVariable

    # Greet the user if it appears this is their first time using wxBanker.
    config = wx.Config.Get()
    firstTime = not config.ReadBool("RUN_BEFORE")
    if firstTime:
        Publisher.sendMessage("first run")
        config.WriteBool("RUN_BEFORE", True)

    return bankController.wxApp


def main():
    app = init()
    app.TopWindow.Show()

    import sys
    if '--inspect' in sys.argv:
        import wx.lib.inspection
        wx.lib.inspection.InspectionTool().Show()

    app.MainLoop()


if __name__ == "__main__":
    main()
