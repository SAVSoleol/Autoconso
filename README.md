# SOLEOL · Simulateur solaire

Application Python / Streamlit qui reprend la maquette : paramètres à gauche,
cartes d'autoconsommation et d'autonomie avant/après, barres de progression,
répartition de l'alimentation du bâtiment et tableau des flux.

## Mise en ligne avec GitHub et Streamlit

1. Décompresser l'archive.
2. Déposer **tout le contenu** dans le dépôt GitHub : `app.py`, les autres modules
   Python, `styles.css`, `requirements.txt` et le dossier `.streamlit`.
   Le dossier `.streamlit` contient le thème ; il faut le conserver.
3. Dans [Streamlit Community Cloud](https://share.streamlit.io/), créer une app
   à partir de ce dépôt et choisir **`app.py`** comme fichier principal.
4. Sélectionner **Python 3.12 ou 3.14** dans les paramètres avancés puis lancer le déploiement.
   Les dépendances incluent pandas 2.3.3, compatible avec Python 3.14, afin d'éviter
   la compilation de pandas 2.2.3 sur les nouveaux environnements Streamlit Cloud.

L'application est prête à déployer ; aucun compte ni dépôt n'a été créé ou modifié
pendant sa préparation.

## Exécution sur un ordinateur

Avec Python 3.12, depuis le dossier du programme :

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## Utilisation

1. Importer un export compteur en CSV, XLS ou XLSX, avec les **deux flux réseau**.
   Les valeurs import/export sont toujours tirées du fichier, jamais saisies à la main.
2. Dans « Production photovoltaïque », saisir la production **AC** réellement
   mesurée à la sortie des onduleurs pour chaque mois, en kWh. Saisir explicitement
   **0** pour un mois sans production. La consommation totale est calculée.
3. Activer ou désactiver la simulation de batterie ; régler la capacité utile,
   la puissance et le rendement aller-retour.
4. Les résultats se mettent à jour automatiquement. Les cycles équivalents sont
   calculés à partir des flux, et ne sont pas un réglage arbitraire.
5. Ouvrir « Détail mensuel et export » pour télécharger les bilans CSV.
6. Ouvrir « Rapport PDF », renseigner le client, le site et éventuellement une
   référence d'étude, puis cliquer sur **Télécharger le rapport PDF**.
   Le document est actualisé dès que les résultats ou les coordonnées changent.

## Rapport PDF

Le rapport reprend la maquette validée :

- Page 1 : identité du client, période, scénario, bilan, taux avant/après batterie,
  alimentation du bâtiment et conclusion.
- Page 2 : flux d'énergie, paramètres de batterie, qualité des données et méthode.
- Les avertissements de lecture et, si nécessaire, la liste complète des sources
  sont ajoutés en annexe. Des coordonnées particulièrement longues peuvent
  également allonger le document ; le contenu n'est pas coupé.

Le rapport utilise les valeurs calculées par l'application. Il ne contient aucune
valeur client codée en dur, aucun calcul financier et aucune recommandation de
rentabilité. Les bilans estimatifs, les productions prévisionnelles, les données
de démonstration et les batteries existantes sont signalés dans le PDF.

`exemples/rapport_exemple.pdf` montre un rapport réellement généré sur les données
synthétiques de l'exemple, avec une batterie de 30 kWh utiles et 10 kW.

### Mettre à jour une application déjà installée

Déposer ou remplacer les éléments suivants à la racine du dépôt GitHub :

1. `app.py`
2. `report.py`
3. `requirements.txt` (avec pandas 2.3.3 et reportlab 4.4.9)
4. Le dossier **`assets`**, avec ses deux polices et leur licence.

Le dossier `assets/fonts` est indispensable : les polices sont embarquées dans le
PDF pour conserver les accents et la présentation. La génération se fait en
mémoire avec ReportLab, sans service externe, navigateur ni outil système à
installer sur Streamlit Cloud. Les dépendances sont compatibles avec Python 3.14.

« Voir un exemple » charge une année synthétique complète : production 12’000 kWh,
import 5’000 kWh et export 7’000 kWh. Les taux initiaux sont 41,7 % et 50,0 %.
Les taux après batterie sont issus de la simulation temporelle, et diffèrent
donc de ceux de la première maquette calculée avec 150 cycles imposés.
L'exemple est clairement identifié ; il n'est jamais présenté comme des mesures client.

Les fichiers `exemples/courbe_demo_2025.csv` et `exemples/production_demo_2025.csv`
permettent également de tester le parcours d'import manuel. Dans la courbe
d'exemple, l'horodatage indique la **fin** de l'intervalle, en UTC avec offset.

## Format générique de la courbe

```csv
Date;Import réseau (kWh);Export réseau (kWh)
2025-01-01T00:15:00+01:00;0,25;0
2025-01-01T00:30:00+01:00;0,30;0
```

- Une ligne par intervalle, à pas constant. Les exports avec offsets UTC sont préférables.
- Si les colonnes sont des puissances moyennes en kW ou W, le programme les
  convertit en énergie à partir de la durée de mesure.
- L'unité et le début/la fin d'intervalle se règlent dans « Réglages des courbes ».
- Plusieurs fichiers sont acceptés s'ils concernent le même compteur, sans
  chevauchement et avec le même pas. La confirmation se trouve dans les réglages.
- Les lecteurs existants Groupe E, Romande Energie, SolarEdge, Fronius et Huawei
  sont conservés. Un fichier dont les en-têtes ne permettent pas d'identifier
  les deux flux est refusé avec un message explicite.
- Les heures suisses sont traitées avec le fuseau Europe/Zurich, passages
  été/hiver compris. Un index cumulé Huawei est différencié avant simulation.

## Calculs et interprétation

Avant batterie :

- Solaire direct = production PV − injection réseau.
- Consommation du bâtiment = solaire direct + import réseau.
- Autoconsommation = solaire direct / production PV.
- Autonomie = solaire direct / consommation du bâtiment.

Après batterie :

- La consommation du bâtiment reste la même.
- Autoconsommation = (production PV − injection après batterie) / production PV.
  Elle inclut les pertes de stockage et le solaire encore stocké à la fin.
- Autonomie = (solaire direct + restitution AC de la batterie) / consommation du bâtiment.
  Les pertes et le stock final ne sont pas crédités au bâtiment.
- La capacité est la capacité **utile**, avant pertes de conversion. Une seconde
  réserve de SOC n'est pas retranchée. Les rendements de charge et de décharge
  sont chacun la racine carrée du rendement aller-retour.
- L'énergie utilisable initiale est nulle. La recharge réseau et la revente
  depuis la batterie sont exclues. La batterie charge uniquement sur l'export
  mesuré et décharge uniquement pour réduire l'import mesuré.
- Charge et décharge partagent la limite de puissance à chaque pas. Quand les
  deux flux sont présents dans un même intervalle, la décharge précède la charge.
  La chronologie réelle à l'intérieur de cet intervalle reste inconnue.
- Le tableau distingue les pertes de conversion, le stock final DC et,
  si nécessaire, le stock non transféré entre segments séparés par des trous.

Avec une batterie **déjà présente** dans les mesures : cocher cette situation
dans les réglages. Aucune seconde simulation n'est faite. Les taux sont alors
affichés comme **apparents**, car les seules mesures compteur ne permettent pas
d'isoler les pertes, les variations de stock ni une éventuelle recharge réseau.

## Qualité et période de mesure

Par défaut, les données manquantes et les mois incomplets bloquent le bilan :
ils ne sont pas silencieusement remplacés par des zéros ou annualisés.

Deux options explicites sont disponibles :

- **Segments mesurés malgré les trous** : seuls les intervalles connus sont
  analysés. Après chaque trou, la batterie repart sans stock utilisable ; le stock
  abandonné est comptabilisé séparément.
- **Prorata des mois incomplets** : la production totale mensuelle est multipliée
  par le pourcentage d'heures mesurées du mois. Il s'agit d'une **estimation**,
  car les heures manquantes peuvent contenir plus ou moins de soleil que la moyenne.

Les résultats concernent la période affichée, sans extrapolation automatique à
l'année. Les mois et les années ne sont jamais mélangés. La cohérence
production/export est contrôlée pour chaque mois avant le bilan total.

Si la production saisie est prévisionnelle (PVsyst), le bilan est annoncé comme
estimatif. Dans PVsyst, utiliser l'énergie AC des onduleurs ; vérifier que le
champ choisi représente la production PV totale, et non uniquement le surplus
vendu au réseau d'une installation en autoconsommation.

## Vérification

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Les tests couvrent les bilans et les pertes, les limites de batterie, les mois
incomplets, les changements d'heure, les imports CSV/XLSX et le parcours Streamlit.

## Fichiers

- `app.py` : saisies, import, contrôles et page Streamlit.
- `ui.py`, `styles.css`, `.streamlit/config.toml` : rendu de la maquette.
- `loaders.py` : lecture et normalisation des courbes existantes.
- `simulation.py` : moteur de batterie existant, sans recharge réseau.
- `energy_metrics.py` : bilans corrigés, contrôle mensuel et tableaux de résultats.
- `demo.py` : année synthétique pour tester l'application.
- `report.py`, `assets/fonts/` : génération du rapport PDF et polices intégrées.

Les fichiers sont traités dans la mémoire et des dossiers temporaires du serveur.
Le programme n'écrit pas les fichiers client dans le dépôt GitHub et ne les
transmet à aucun service d'analyse externe.
