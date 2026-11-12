def wallet_context(request):
    return {"wallet_account": request.session.get("wallet")}
