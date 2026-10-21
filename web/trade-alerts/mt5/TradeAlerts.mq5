//+------------------------------------------------------------------+
//| TradeAlerts.mq5                                                  |
//| Sends each trade action to the trade alerts receiver, which      |
//| posts it to the Telegram channel and the WhatsApp Channel.       |
//|                                                                  |
//| Events: pending order placed / cancelled, position opened,       |
//| SL/TP changed, position closed (TP, SL, manual, partial).        |
//| Sent: symbol, direction, prices, SL/TP, points.                  |
//| Never sent: lot size, balance, profit in money.                  |
//|                                                                  |
//| Setup: Tools > Options > Expert Advisors > tick "Allow           |
//| WebRequest for listed URL" and add the receiver's address        |
//| (https://trade-alerts-owner.vercel.app). Attach this EA to any   |
//| one chart; it reports trades on every symbol of the account.     |
//+------------------------------------------------------------------+
#property copyright "Owner"
#property version   "1.00"
#property description "Posts each trade to Telegram and WhatsApp through the trade alerts receiver."

input string WebhookUrl        = "https://trade-alerts-owner.vercel.app/api/trade"; // Receiver address
input string WebhookSecret     = "";    // Secret (TRADE_WEBHOOK_SECRET from the project's .env file)
input bool   PostPendingOrders = true;  // Also post pending orders placed and cancelled

struct PositionLevels
  {
   ulong             position;
   double            sl;
   double            tp;
  };
PositionLevels g_levels[];

//+------------------------------------------------------------------+
int OnInit()
  {
   if(StringLen(WebhookSecret) < 16)
     {
      Alert("TradeAlerts: paste the secret into the WebhookSecret input first.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   // Remember the SL/TP of positions that are already open, so only real changes are posted.
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0)
         Remember(ticket, PositionGetDouble(POSITION_SL), PositionGetDouble(POSITION_TP));
     }
   Print("TradeAlerts: running. Trades on this account will be posted.");
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
string Num(const double value, const int digits)
  {
   return(DoubleToString(value, digits));
  }

//+------------------------------------------------------------------+
string PendingSide(const ENUM_ORDER_TYPE type)
  {
   switch(type)
     {
      case ORDER_TYPE_BUY_LIMIT:       return("BUY LIMIT");
      case ORDER_TYPE_SELL_LIMIT:      return("SELL LIMIT");
      case ORDER_TYPE_BUY_STOP:        return("BUY STOP");
      case ORDER_TYPE_SELL_STOP:       return("SELL STOP");
      case ORDER_TYPE_BUY_STOP_LIMIT:  return("BUY STOP LIMIT");
      case ORDER_TYPE_SELL_STOP_LIMIT: return("SELL STOP LIMIT");
      default:                         return("");
     }
  }

//+------------------------------------------------------------------+
int Find(const ulong position)
  {
   for(int i = 0; i < ArraySize(g_levels); i++)
      if(g_levels[i].position == position)
         return(i);
   return(-1);
  }

void Remember(const ulong position, const double sl, const double tp)
  {
   int i = Find(position);
   if(i < 0)
     {
      i = ArraySize(g_levels);
      ArrayResize(g_levels, i + 1);
      g_levels[i].position = position;
     }
   g_levels[i].sl = sl;
   g_levels[i].tp = tp;
  }

void Forget(const ulong position)
  {
   int i = Find(position);
   if(i < 0)
      return;
   int last = ArraySize(g_levels) - 1;
   g_levels[i] = g_levels[last];
   ArrayResize(g_levels, last);
  }

//+------------------------------------------------------------------+
bool Send(const string json)
  {
   char data[];
   char reply[];
   string replyHeaders;
   StringToCharArray(json, data, 0, StringLen(json), CP_UTF8);
   string headers = "Content-Type: application/json\r\nX-Trade-Secret: " + WebhookSecret + "\r\n";
   ResetLastError();
   int code = WebRequest("POST", WebhookUrl, headers, 8000, data, reply, replyHeaders);
   if(code == -1)
     {
      Print("TradeAlerts: could not reach the receiver (error ", GetLastError(),
            "). Add the address in Tools > Options > Expert Advisors.");
      return(false);
     }
   if(code != 200)
      Print("TradeAlerts: the receiver answered ", code, ": ", CharArrayToString(reply, 0, WHOLE_ARRAY, CP_UTF8));
   return(code == 200);
  }

//+------------------------------------------------------------------+
void SendLevels(const string event, const string symbol, const string side, const ulong ticket,
                const double price, const double sl, const double tp, const double openPrice)
  {
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   string json = StringFormat("{\"event\":\"%s\",\"symbol\":\"%s\",\"side\":\"%s\",\"ticket\":\"%I64u\","
                              "\"price\":%s,\"sl\":%s,\"tp\":%s,\"open_price\":%s}",
                              event, symbol, side, ticket, Num(price, digits), Num(sl, digits), Num(tp, digits),
                              Num(openPrice, digits));
   Send(json);
  }

//+------------------------------------------------------------------+
double OpenPrice(const ulong position, const double fallback)
  {
   if(!HistorySelectByPosition(position))
      return(fallback);
   for(int i = 0; i < HistoryDealsTotal(); i++)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(HistoryDealGetInteger(deal, DEAL_ENTRY) == DEAL_ENTRY_IN)
         return(HistoryDealGetDouble(deal, DEAL_PRICE));
     }
   return(fallback);
  }

//+------------------------------------------------------------------+
void OnDeal(const ulong deal)
  {
   if(!HistoryDealSelect(deal))
      return;
   ENUM_DEAL_TYPE type = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal, DEAL_TYPE);
   if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
      return;
   ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal, DEAL_ENTRY);
   string symbol   = HistoryDealGetString(deal, DEAL_SYMBOL);
   ulong position  = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
   double price    = HistoryDealGetDouble(deal, DEAL_PRICE);
   int digits      = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   if(entry == DEAL_ENTRY_IN)
     {
      string side = (type == DEAL_TYPE_BUY) ? "BUY" : "SELL";
      double sl = HistoryDealGetDouble(deal, DEAL_SL);
      double tp = HistoryDealGetDouble(deal, DEAL_TP);
      if(PositionSelectByTicket(position))
        {
         sl = PositionGetDouble(POSITION_SL);
         tp = PositionGetDouble(POSITION_TP);
        }
      Remember(position, sl, tp);
      SendLevels("opened", symbol, side, position, price, sl, tp, price);
      return;
     }

   if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
     {
      // A closing deal goes the opposite way to the position it closes.
      string side = (type == DEAL_TYPE_SELL) ? "BUY" : "SELL";
      double openPrice = OpenPrice(position, price);
      ENUM_DEAL_REASON why = (ENUM_DEAL_REASON)HistoryDealGetInteger(deal, DEAL_REASON);
      string reason = "manual";
      if(why == DEAL_REASON_TP)          reason = "tp";
      else if(why == DEAL_REASON_SL)     reason = "sl";
      else if(why == DEAL_REASON_SO)     reason = "stopout";
      else if(why == DEAL_REASON_EXPERT) reason = "expert";
      bool stillOpen = PositionSelectByTicket(position);
      if(stillOpen)
         reason = "partial";
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      double points = (point > 0) ? (price - openPrice) / point * ((side == "BUY") ? 1.0 : -1.0) : 0.0;
      string json = StringFormat("{\"event\":\"closed\",\"symbol\":\"%s\",\"side\":\"%s\",\"ticket\":\"%I64u\","
                                 "\"open_price\":%s,\"close_price\":%s,\"points\":%s,\"reason\":\"%s\"}",
                                 symbol, side, position, Num(openPrice, digits), Num(price, digits),
                                 DoubleToString(points, 0), reason);
      Send(json);
      if(!stillOpen)
         Forget(position);
     }
  }

//+------------------------------------------------------------------+
void OnPositionChange(const ulong position)
  {
   if(!PositionSelectByTicket(position))
      return;
   double sl = PositionGetDouble(POSITION_SL);
   double tp = PositionGetDouble(POSITION_TP);
   int i = Find(position);
   if(i >= 0 && g_levels[i].sl == sl && g_levels[i].tp == tp)
      return;   // nothing changed that readers would see
   bool known = (i >= 0);
   Remember(position, sl, tp);
   if(!known)
      return;   // first sight of this position: the "opened" post already showed its levels
   string symbol = PositionGetString(POSITION_SYMBOL);
   string side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
   double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   SendLevels("modified", symbol, side, position, openPrice, sl, tp, openPrice);
  }

//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
  {
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
     {
      OnDeal(trans.deal);
      return;
     }
   if(trans.type == TRADE_TRANSACTION_POSITION)
     {
      OnPositionChange(trans.position);
      return;
     }
   if(!PostPendingOrders)
      return;
   string side = PendingSide(trans.order_type);
   if(side == "")
      return;   // market orders are reported by their deal
   if(trans.type == TRADE_TRANSACTION_ORDER_ADD)
      SendLevels("pending_placed", trans.symbol, side, trans.order, trans.price, trans.price_sl, trans.price_tp, trans.price);
   else if(trans.type == TRADE_TRANSACTION_ORDER_DELETE &&
           (trans.order_state == ORDER_STATE_CANCELED || trans.order_state == ORDER_STATE_EXPIRED))
      SendLevels("pending_cancelled", trans.symbol, side, trans.order, trans.price, trans.price_sl, trans.price_tp, trans.price);
  }
//+------------------------------------------------------------------+
