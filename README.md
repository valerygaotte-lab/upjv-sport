# upjv-sport
site 

## Événements « Autour d'Amiens » (mise à jour automatique)

- `actualiser.py` lit les matchs à domicile des **Gothiques d'Amiens** (billetterie officielle) et de l'**Amiens SC**
  (calendrier FFF via TheFootData) et écrit `evenements.json`.
- Le workflow GitHub Actions « Actualiser les événements » le lance tous les 3 jours (et à la demande : onglet
  Actions › Actualiser les événements › Run workflow ; cocher « diagnostic » pour voir ce que renvoient les sites).
- La page d'accueil lit `evenements.json` directement dans le dépôt (le dépôt doit rester public), sinon la copie publiée à côté de la page.
- Amiens SC : seuls les matchs dont le jour et l'heure sont programmés sont affichés ; les autres sont annoncés « à confirmer ».
- Un match impossible à lire automatiquement peut être ajouté dans `evenements_manuels.json`.
- Tests hors ligne : `python3 -m unittest discover -s tests`
