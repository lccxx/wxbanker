#!/usr/bin/env python
#
#    https://launchpad.net/wxbanker
#    account.py: Copyright 2007-2010 Mike Rooney <mrooney@ubuntu.com>
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

from wx.lib.pubsub import Publisher #@UnresolvedImport

from wxbanker.bankobjects.ormobject import ORMObject
from wxbanker.bankobjects.transaction import Transaction
from wxbanker.bankobjects.recurringtransaction import RecurringTransaction
from wxbanker import currencies, bankexceptions, debug
from wxbanker.mint.api import Mint

import datetime

class Account(ORMObject):
    ORM_TABLE = "accounts"
    ORM_ATTRIBUTES = ["Name", "Balance", "MintId"]
    
    def __init__(self, store, aID, name, currency=0, balance=0.0, mintId=None):
        ORMObject.__init__(self)
        self.IsFrozen = True
        self.Store = store
        self.ID = aID
        self._Name = name
        self._Transactions = None
        self._RecurringTransactions = []
        self._preTransactions = []
        self.Currency = currency
        self.Balance = balance
        self.MintId = mintId
        self.IsFrozen = False

        Publisher.subscribe(self.onTransactionAmountChanged, "ormobject.updated.Transaction.Amount")

    def ParseAmount(self, strAmount):
        """
        Robustly parse an amount. Remove ANY spaces, so they can be used as padding
        or thousands separators. Find the thing intended as a decimal, if any,
        and replace it with a period, removing anything else that may be a thousands sep.
        """
        # Remove any spaces anywhere.
        strAmount = strAmount.replace(" ", "")

        # Iterate over the string in reverse to locate decimal sep.
        decimalPos = None
        for i in range(min(3, len(strAmount))):
            pos = -(i+1)
            char = strAmount[pos]
            if char in ",." and i <= 2:
                decimalPos = pos + 1
                break
        strAmount = strAmount.replace(",", "")
        strAmount = strAmount.replace(".", "")

        if decimalPos:
            strAmount = strAmount[:decimalPos] + "." + strAmount[decimalPos:]

        return float(strAmount)

    def GetSiblings(self):
        return [account for account in self.Parent if account is not self]

    def SetCurrency(self, currency):
        if type(currency) == int:
            self._Currency = currencies.CurrencyList[currency]()
        else:
            self._Currency = currency

    def GetCurrency(self):
        return self._Currency
    
    def GetCurrentBalance(self):
        """Returns the balance up to and including today, but not transactions in the future."""
        currentBalance = self.Balance
        today = datetime.date.today()
        transactions = self.Transactions[:]
        transactions.sort()
        index = len(transactions) - 1
        while index >= 0 and transactions[index].Date > today:
            currentBalance -= transactions[index].Amount
            index -= 1
        return currentBalance
        
    def GetRecurringTransactions(self):
        return self._RecurringTransactions
        
    def SetRecurringTransactions(self, recurrings):
        self._RecurringTransactions = recurrings

    def GetTransactions(self):
        if self._Transactions is None:
            self._Transactions = self.Store.getTransactionsFrom(self)
            # If transactions were added before this list was pulled,
            # and then an attribute is changed on one of them (Amount/Description/Date), it won't be
            # reflected on the new account at run-time because it has a replacement instance for that transaction.
            # So we need to swap it in.
            if self._preTransactions:
                # Iterate over this first (and thus once) since it is probably larger than _pre.
                for i, newT in enumerate(self._Transactions):
                    for oldT in self._preTransactions:
                        # Compare just IDs otherwise TDD is impossible; new attributes not yet stored
                        # will cause the objects to not be properly replaced and break tests.
                        if oldT.ID == newT.ID:
                            self._Transactions[i] = oldT
                            break

        return self._Transactions

    def GetName(self):
        return self._Name

    def SetName(self, name):
        self.Parent.ThrowExceptionOnInvalidName(name)
        self._Name = name

    def Remove(self):
        self.Parent.Remove(self.Name)

    def AddTransactions(self, transactions, sources=None):
        Publisher.sendMessage("batch.start")
        # If we don't have any sources, we want None for each transaction.
        if sources is None:
            sources = [None for i in range(len(transactions))] #@UnusedVariable
            
        for t, source in zip(transactions, sources):
            self.AddTransaction(transaction=t, source=source)
        Publisher.sendMessage("batch.end")
        
    def AddRecurringTransaction(self, amount, description, date, repeatType, repeatEvery=1, repeatOn=None, endDate=None, source=None):
        # Create the recurring transaction object.
        recurring = RecurringTransaction(None, self, amount, description, date, repeatType, repeatEvery, repeatOn, endDate, source)
        # Store it.
        self.Store.MakeRecurringTransaction(recurring)
        # Add it to our internal list.
        self.RecurringTransactions.append(recurring)
        
        Publisher.sendMessage("recurringtransaction.created", (self, recurring))
        
        return recurring
    
    def RemoveRecurringTransaction(self, recurring):
        # Orphan any recurring children so that they are normal transactions.
        for child in recurring.GetChildren():
            child.RecurringParent = None
            if child.LinkedTransaction:
                child.LinkedTransaction.RecurringParent = None
                
        # Now remove the actual recurring transaction.
        self.RecurringTransactions.remove(recurring)
        self.Store.RemoveRecurringTransaction(recurring)

    def AddTransaction(self, amount=None, description="", date=None, source=None, transaction=None):
        """
        Enter a transaction in this account, optionally making the opposite
        transaction in the source account first.
        """
        Publisher.sendMessage("batch.start")
        
        if transaction:
            # It is "partial" because its ID and parent aren't necessarily correct.
            partialTrans = transaction
            partialTrans.Parent = self
        elif amount is not None:
            # No transaction object was given, we need to make one.
            partialTrans = Transaction(None, self, amount, description, date)
        else:
            raise Exception("AddTransaction: Must provide either transaction arguments or a transaction object.")
        
        if source:
            otherTrans = source.AddTransaction(-1 * partialTrans.Amount, partialTrans._Description, partialTrans.Date)
            
        transaction = self.Store.MakeTransaction(self, partialTrans)

        # If it was a transfer, link them together
        if source:
            transaction.LinkedTransaction = otherTrans
            otherTrans.LinkedTransaction = transaction

        # Don't append if there aren't transactions loaded yet, it is already in the model and will appear on a load. (LP: 347385).
        if self._Transactions is not None:
            self.Transactions.append(transaction)
        else:
            # We will need to do some magic with these later when transactions are loaded.
            self._preTransactions.append(transaction)

        Publisher.sendMessage("transaction.created", (self, transaction))

        # Update the balance.
        self.Balance += transaction.Amount
        Publisher.sendMessage("batch.end")

        if source:
            return transaction, otherTrans
        else:
            return transaction

    def RemoveTransaction(self, transaction):
        return self.RemoveTransactions([transaction])

    def RemoveTransactions(self, transactions, removeLinkedTransactions=True):
        """
        By default removing a transaction removes the linked transaction from its account too.
        However when removeLinkedTransactions is False, only transactions in this account
        will be removed, such as from Account.Purge().
        """
        Publisher.sendMessage("batch.start")
        # Return the sources, if any, of the removed transactions, in case we are moving for example.
        sources = []
        
        # Accumulate the difference and update the balance just once. Cuts 33% time of removals.
        difference = 0
        
        # Make a copy of the list, to protect against getting passed self.Transactions itself.
        # Otherwise when we remove from self.Transactions, we'd end up iterating over every other transaction! (LP: #605591)
        transactions = transactions[:]
        
        for transaction in transactions:
            if transaction not in self.Transactions:
                raise bankexceptions.InvalidTransactionException("Transaction does not exist in account '%s'" % self.Name)

            # If this transaction was a transfer, delete the other transaction as well.
            if transaction.LinkedTransaction:
                link = transaction.LinkedTransaction
                sources.append(link.Parent)
                # Kill the other transaction's link to this one, otherwise this is quite recursive.
                link.LinkedTransaction = None
                # Delete the linked transaction from its account as well, if we should.
                if removeLinkedTransactions:
                    link.Remove()
            else:
                sources.append(None)

            # Now remove this transaction.
            self.Store.RemoveTransaction(transaction)
            transaction.Parent = None
            self.Transactions.remove(transaction)
            difference += transaction.Amount

        # Update the balance.
        self.Balance -= difference
        # Send the message for all transactions at once, cuts _97%_ of time! OLV is slow here I guess.
        Publisher.sendMessage("transactions.removed", (self, transactions))
        Publisher.sendMessage("batch.end")
        return sources
    
    def Purge(self):
        """
        Call this when the account is being deleted and should purge itself.
        This will delete all transactions but NOT any linked transactions in other accounts,
        because when you close a bank account it doesn't erase the historical transfers in other accounts.
        """
        self.RemoveTransactions(self.Transactions, removeLinkedTransactions=False)

    def MoveTransaction(self, transaction, destAccount):
        self.MoveTransactions([transaction], destAccount)

    def MoveTransactions(self, transactions, destAccount):
        Publisher.sendMessage("batch.start")
        sources = self.RemoveTransactions(transactions)
        destAccount.AddTransactions(transactions, sources)
        Publisher.sendMessage("batch.end")
        
    def GetMintId(self):
        return self._MintId
    
    def SetMintId(self, mintId):
        if mintId is not None:
            mintId = int(mintId)
        self._MintId = mintId
        
    def IsMintEnabled(self):
        return self.MintId is not None
    
    def IsOutOfSync(self):
        """Returns true only if explicitly out of sync: set to sync and able to get a balance, and balance is different."""
        if self.IsMintEnabled():
            if Mint.IsLoggedIn():
                return not self.IsInSync()
        return False
        
    def IsInSync(self):
        if self.MintId is None:
            raise bankexceptions.MintIntegrationException("This account has no MintId.")
        
        try:
            mintBalance = Mint.GetAccountBalance(self.MintId)
        except:
            return False
        
        inSync = abs(mintBalance - self.CurrentBalance) < .001
        return inSync
    
    def GetSyncString(self):
        if self.MintId is None:
            raise bankexceptions.MintIntegrationException("This account has no MintId.")
        
        try:
            item = Mint.GetAccount(self.MintId)
        except:
            syncString = "Unable to synchronize with Mint.com"
        else:
            syncString = "%s: %s" % (item['name'], self.float2str(item['balance']))
        
        return syncString

    def onTransactionAmountChanged(self, message):
        transaction = message.data
        if transaction.Parent == self:
            debug.debug("Updating balance because I am %s: %s" % (self.Name, transaction))
            self.Balance = sum((t.Amount for t in self.Transactions))
        else:
            debug.debug("Ignoring transaction because I am %s: %s" % (self.Name, transaction))

    def float2str(self, *args, **kwargs):
        return self.Currency.float2str(*args, **kwargs)

    def __cmp__(self, other):
        return cmp(self.Name, other.Name)

    def __eq__(self, other):
        if other is None:
            return False
        
        return (
            self.Name == other.Name and
            self.Balance == other.Balance and
            self.Currency == other.Currency and
            self.Transactions == other.Transactions and
            self.RecurringTransactions == other.RecurringTransactions
        )

    Name = property(GetName, SetName)
    Transactions = property(GetTransactions)
    RecurringTransactions = property(GetRecurringTransactions)
    Currency = property(GetCurrency, SetCurrency)
    MintId = property(GetMintId, SetMintId)
    CurrentBalance = property(GetCurrentBalance)