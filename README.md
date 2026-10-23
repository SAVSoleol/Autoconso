# Calculateur autoconsommation et autonomie PV

Application Streamlit distincte du simulateur de dimensionnement de batterie.

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## Saisie

1. Importer la courbe de charge avec **import réseau** et **export / injection
   réseau**.
2. Saisir la production photovoltaïque **AC** de janvier à décembre : production
   relevée à la sortie des onduleurs ou `E_Grid` de PVsyst. Ne pas saisir les
   kWc ni une production DC.
3. Choisir l'une des situations batterie :

   - **Aucune batterie** : calcul direct sur les flux mesurés.
   - **Batterie existante déjà incluse dans la courbe** : calcul direct du
     fonctionnement réel. La capacité est seulement affichée ; elle n'est pas
     simulée une seconde fois.
   - **Batterie à simuler sur une courbe sans batterie** : saisir capacité,
     puissance, rendement et SOC minimum. Le programme compare alors les taux
     sans batterie et avec la batterie simulée.

Une batterie ne peut pas être simulée correctement avec la seule capacité : sa
puissance de charge/décharge et son rendement sont aussi nécessaires.

## Résultats

`Taux d'autoconsommation = (Production PV − injection réseau) / Production PV`

`Taux d'autonomie énergétique = solaire utilisé sur site / (solaire utilisé sur site + import réseau)`

Le détail mensuel est aussi affiché. Les productions mensuelles sont ramenées à
la durée réellement couverte par la courbe, afin de ne pas compter un mois entier
lorsque seules quelques journées sont présentes.

Pour une batterie déjà en service, les deux taux proviennent du bilan compteur.
Les pertes propres de la batterie ne peuvent être isolées précisément sans son
journal de charge/décharge ; elles restent donc incluses dans le bilan du site.
