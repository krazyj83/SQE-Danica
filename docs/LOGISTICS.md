# Logistics

Every airfield on both sides, and the carrier, holds **fuel**, **ammunition** and **spares**, in tonnes. Open the **Logistics** page to see them. It has bars per base and a list of last night's shipments.

## What happens each day

1. **Flying uses stock.** Each package that flew charges fuel and ammunition to the bases its flights took off from. A full fuel load plus 30% for taxi and reserve; about 1 t of ammunition per aircraft for ground attack and 0.25 t for air-to-air (WWII aircraft use 40% of that). Red fields burn fuel for their own patrols: fighters fly most, strike aircraft less, strategic bombers rarely.
2. **Overnight, the supply chain ships forward.** Each side's lift is sized to its own air force.
   - With everything intact it ships about 115% of what the force uses.
   - The rear area (or the sea, for the fleet) covers 65% of that on its own.
   - Depots cover the other 50%, scaled by their health. For Red these are the fuel and ammunition targets on the map; for Blue, the forward supply depot.
   - Shipments go to the bases that need them most. A Blue shipment that crosses live enemy air defences, or a Red one under Allied air superiority, loses part of its load.
3. **Replacements use spares.** New aircraft (1.5 t each) come out of the base's spares, and none arrive if the spares run out.

## What stock does

- **A squadron can't fly more sorties than its base's fuel and ammunition allow.** The daily tasking order respects it. Squadrons sharing a base split the stock by what they burn, so an F-15 squadron gets more fuel than the F-16 squadron next to it.
- **A Red field below 15% fuel can't launch** fighters, escorts, reinforcements or raids.
- **The ground war's supply** also reflects how well stocked each side's forward bases are.
- **Striking Red's depots and fuel sites matters.** With them gone, Red's forward fields run down, its replacements dry up and its air force shrinks.

A fallen Blue field or a cratered Red field receives nothing.

Campaigns from before logistics get a full ledger the first time they're opened.

## Cargo runs you fly

Every day the tasking order includes up to two **cargo runs** to the neediest airfields: "Deliver supplies to Kutaisi AB". A cargo package is a transport flight from its own field to the destination, plus a fighter escort when enemy fighters can reach the route. It needs no AWACS or tanker.

- **Fly the transport.** The C-130J-30 carries 18 t, the CH-47F 10 t, the Mi-8MT 3 t and the UH-1H 1.5 t. Raise a new squadron of one in New Campaign, or take the Caucasus pack's VMGR-252 (C-130J, Batumi) or B Co 1-214th (CH-47F, Kobuleti). Helicopters fly the route at 500 ft above the ground.
- **Or fly the escort** in your fighter, if the package has one.
- **What counts:** each transport that **lands at the destination** and survives delivers its load (half fuel, a third ammunition, the rest spares). The mission hook records where every aircraft touched down: the airbase name, with a 6 km fallback on position. A transport shot down or landed elsewhere delivers nothing.
- **The AI flies the runs you don't.** Each transport gets through unless the route bites, with the same losses a shipment suffers; an escort halves them.

Cargo is planned after the combat packages, numbered after them and launched through the day, so it never takes a combat package's aircraft.

## Red convoys you can catch

Each night Red's three biggest road shipments travel as **supply convoys** and arrive the next night. Until then each is a target on the road, 60% of the way from its depot to its field: "Interdict the supply convoy to Carpiquet". What a convoy delivers is scaled by what is left of it, so a convoy hit to 30% delivers 30% and a destroyed one delivers nothing. Smaller loads go by back roads and arrive the same night. On WWII maps the convoy is Opel Blitz trucks and Sd.Kfz. 2 tractors with a 2 cm flak.

On Normandy there are no flyable transports yet, because pydcs's C-47 is AI-only. So there are no cargo runs there, but convoy interdiction works.
