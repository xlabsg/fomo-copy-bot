# Paper instance template

The paper bot runs the same `bot.py` from a sibling directory `../rh-copybot-paper`
(see the main README). To recreate it:

```bash
mkdir -p ../rh-copybot-paper/data
cp paper-instance/config.json paper-instance/paper.sh ../rh-copybot-paper/
ln -s ../rh-copybot/wallets.json ../rh-copybot-paper/wallets.json
```
